"""Chimera PredMark — FastAPI service.

Deployed to Cloud Run with --no-allow-unauthenticated; the dashboard reaches
it through the portal proxy (CHI-POL-004). Scanning is driven by Cloud
Scheduler hitting POST /scan, not by an in-process loop.

The endpoint contract is in the README. The rule that shapes this file: no
endpoint returns a credential value under any circumstances. /settings
returns masked confirmations, and there is no read counterpart to
PUT /settings/credentials.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config
from logging_setup import configure_logging, log
from risk import exposure as exposure_module
from risk import killswitch
from scan import runner
from settings import secrets, store
from settings.schema import Settings
from storage import gcs
from storage import trades as trade_store
from venues.pmxt_client import PmxtClient
from venues.registry import VENUES, describe_venues

configure_logging()
logger = logging.getLogger("predmark")

# One client for the process lifetime, holding the connection pool to the
# sidecar. Only one scan runs at a time — Cloud Scheduler will not overlap
# invocations at any sane cadence, and this lock makes that explicit.
pmxt_client = PmxtClient()
scan_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pmxt_client.start()
    ready = await pmxt_client.wait_until_ready(attempts=30, delay=1.0)
    log(
        logger,
        logging.INFO if ready else logging.ERROR,
        "pmxt sidecar ready" if ready else "pmxt sidecar did not come up",
        url=config.PMXT_URL,
        revision=config.REVISION,
    )
    yield
    await pmxt_client.close()


app = FastAPI(
    title="Chimera PredMark",
    description="Prediction market arbitrage — detection and execution",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)


def _actor(request: Request) -> str:
    """Who made this change, for the audit trail.

    The portal proxy is the only route in, so identity is whatever it
    forwards. Recorded as-is: an honest "unknown" beats a fabricated
    username.
    """
    for header in ("x-goog-authenticated-user-email", "cf-access-authenticated-user-email", "x-forwarded-user"):
        value = request.headers.get(header)
        if value:
            return value.split(":")[-1]
    return "dashboard"


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness. Deliberately cheap — no Firestore, no venue calls."""
    return {"status": "ok", "service": config.SERVICE_NAME}


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: the sidecar must be answering for a scan to be possible."""
    sidecar = await pmxt_client.health()
    body = {
        "status": "ready" if sidecar else "degraded",
        "pmxt_sidecar": sidecar,
        "revision": config.REVISION,
    }
    return JSONResponse(body, status_code=200 if sidecar else 503)


@app.get("/info")
async def info() -> dict[str, Any]:
    settings = store.load_settings()
    state = store.load_state()
    return {
        "service": config.SERVICE_NAME,
        "revision": config.REVISION,
        "build_sha": config.BUILD_SHA,
        "region": config.REGION,
        "project": config.PROJECT_ID,
        "bucket": config.GCS_BUCKET,
        "venues": {
            name: {
                "enabled": venue.enabled,
                "label": venue.label,
                "credentials_ready": secrets.credentials_ready(name),
            }
            for name, venue in settings.venues.items()
        },
        "trading_enabled": settings.execution.trading_enabled,
        "dry_run": settings.execution.dry_run,
        "kill_switch": settings.risk.kill_switch,
        "last_scan_at": state.get("last_scan_at"),
        "last_scan_id": state.get("last_scan_id"),
        "pmxt_url": config.PMXT_URL,
        "self_hosted_pmxt": True,
    }


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


@app.post("/scan")
async def scan(
    execute: bool = Query(True, description="Run execution as well as detection"),
    settle: bool = Query(True, description="Check open trades for resolution first"),
) -> dict[str, Any]:
    """One scan cycle. Called by Cloud Scheduler job `predmark-scan`.

    Returns 409 rather than queueing if a scan is already running: two
    concurrent scans would double-count exposure and could place the same
    trade twice.
    """
    if scan_lock.locked():
        raise HTTPException(status_code=409, detail="a scan is already running")

    async with scan_lock:
        settings = store.load_settings()
        settlement: dict[str, Any] = {}
        if settle and settings.execution.trading_enabled:
            try:
                settlement = await runner.settle_open_trades(pmxt_client, settings)
            except Exception as exc:  # noqa: BLE001 - settlement must not block a scan
                log(logger, logging.ERROR, "settlement check failed", error=str(exc))
                settlement = {"error": str(exc)}

        result = await runner.run_scan(pmxt_client, settings, execute=execute)
        summary = result.summary()
        summary["settlement"] = settlement
        log(logger, logging.INFO, "scan complete", **{
            k: v for k, v in summary.items() if k not in {"errors", "trade_ids"}
        })
        return summary


# --------------------------------------------------------------------------
# Opportunities and trades
# --------------------------------------------------------------------------


@app.get("/opportunities")
async def opportunities(
    limit: int = Query(100, ge=1, le=500),
    tradeable_only: bool = False,
    min_margin: Optional[float] = None,
    resolution_status: Optional[str] = Query(
        None, description="MATCHED | DIFFERS | UNVERIFIED"
    ),
) -> dict[str, Any]:
    items = trade_store.list_opportunities(
        limit=limit,
        tradeable_only=tradeable_only,
        min_margin=min_margin,
        resolution_status=resolution_status,
    )
    state = store.load_state()
    return {
        "opportunities": items,
        "count": len(items),
        "last_scan_at": state.get("last_scan_at"),
        "last_scan_id": state.get("last_scan_id"),
    }


@app.get("/trades")
async def trades(
    status: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    days: int = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    items = trade_store.list_trades(status=status, limit=limit)
    return {
        "trades": items,
        "count": len(items),
        "pnl": trade_store.pnl_summary(days=days),
    }


@app.get("/trades/{trade_id}")
async def trade_detail(trade_id: str) -> dict[str, Any]:
    trade = trade_store.get_trade(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail=f"trade {trade_id} not found")
    return trade


@app.post("/trades/{trade_id}/unwind")
async def unwind_trade(trade_id: str, request: Request) -> dict[str, Any]:
    """Close every filled leg of a trade at market, now."""
    from execution import engine as execution_engine

    settings = store.load_settings()
    actor = _actor(request)
    log(logger, logging.WARNING, "manual unwind requested", trade_id=trade_id, actor=actor)
    result = await execution_engine.manual_unwind(
        trade_id, pmxt_client, settings, actor=actor
    )
    if not result.get("ok") and "not found" in str(result.get("error", "")):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/positions")
async def positions() -> dict[str, Any]:
    """Live per-venue exposure and balances.

    Exposure is summed across venues, never netted: a YES on one venue and a
    NO on another are two separate holdings with two separate collateral
    requirements.
    """
    settings = store.load_settings()
    balances = await exposure_module.fetch_balances(pmxt_client, settings)
    live = await exposure_module.fetch_positions(pmxt_client, settings)
    report = exposure_module.exposure_report(balances, settings)
    return {
        "exposure": report,
        "positions": live,
        "balances": balances,
        "open_trades": trade_store.open_trades(),
    }


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@app.get("/settings")
async def get_settings() -> dict[str, Any]:
    """Current configuration. Credentials appear as masked confirmations only."""
    settings = store.load_settings()
    return {
        "settings": settings.model_dump(),
        "credentials": secrets.describe_all(),
        "venue_catalogue": describe_venues(),
        "state": store.load_state(),
    }


@app.put("/settings")
async def put_settings(
    request: Request, patch: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Apply a partial settings update. Validated, persisted, audited.

    A credential submitted here is rejected outright rather than quietly
    dropped — a silent drop would leave Charles believing a key was saved.
    """
    if _contains_credential(patch):
        raise HTTPException(
            status_code=400,
            detail=(
                "credentials must be sent to PUT /settings/credentials, which "
                "writes them to Secret Manager. They are never stored in settings."
            ),
        )

    try:
        settings, changes = store.update_settings(patch, actor=_actor(request))
    except Exception as exc:  # noqa: BLE001 - validation failure is a client error
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "settings": settings.model_dump(),
        "changes": changes,
        "credentials": secrets.describe_all(),
    }


def _contains_credential(payload: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower().replace("-", "_") in {
                "private_key",
                "privatekey",
                "api_key",
                "apikey",
                "api_secret",
                "apisecret",
                "signing_key",
                "signingkey",
                "passphrase",
                "secret",
                "credentials",
            }:
                return True
            if _contains_credential(value, depth + 1):
                return True
    return False


class CredentialWrite(BaseModel):
    venue: str = Field(..., description="Venue name, e.g. polymarket")
    key: str = Field(..., description="Credential field, e.g. privateKey")
    value: str = Field(..., min_length=1, description="The secret value")


@app.put("/settings/credentials")
async def put_credential(
    request: Request, payload: CredentialWrite
) -> dict[str, Any]:
    """Write one credential to Secret Manager.

    The value goes in and does not come back. The response confirms it was
    stored and shows the last four characters so Charles can tell one key
    from another — nothing more, on this endpoint or any other.
    """
    if payload.venue not in VENUES:
        raise HTTPException(status_code=400, detail=f"unknown venue {payload.venue}")

    shape_error = secrets.validate_shape(payload.venue, payload.key, payload.value)
    if shape_error:
        raise HTTPException(status_code=400, detail=shape_error)

    try:
        result = secrets.write_secret(payload.venue, payload.key, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except secrets.SecretsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    actor = _actor(request)
    store.write_audit(
        [
            {
                "field": f"credentials.{payload.venue}.{payload.key}",
                "from": "[not shown]",
                "to": "[stored in secret manager]",
            }
        ],
        actor,
        note=f"credential written to {result['secret_id']} version {result['version']}",
    )
    return {"ok": True, **result, "credentials": secrets.describe_all()}


@app.get("/settings/audit")
async def settings_audit(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"audit": store.read_audit(limit=limit)}


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------


class KillRequest(BaseModel):
    engaged: bool = True
    reason: str = ""


@app.post("/kill")
async def kill(request: Request, payload: KillRequest = Body(default=KillRequest())) -> dict[str, Any]:
    """Halt execution immediately, or release the halt.

    Works without a redeploy and without a restart: the flag lives in
    Firestore and every execution decision reads it fresh. Scanning
    continues either way.
    """
    actor = _actor(request)
    if payload.engaged:
        return killswitch.engage(payload.reason or "engaged from dashboard", actor)
    return killswitch.release(payload.reason or "released from dashboard", actor)


@app.get("/kill")
async def kill_status() -> dict[str, Any]:
    return killswitch.status()


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


@app.get("/history")
async def history(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Aggregates for the history view.

    Opportunity frequency, spread distribution, and — the number worth
    watching — the proportion of candidate pairs that fail resolution
    matching. A high proportion means the matcher is finding questions that
    look alike and settle differently, which is the risk this whole thing is
    built around.
    """
    scans = gcs.list_recent_scans(limit=days * 12)
    pnl = trade_store.pnl_summary(days=days)
    current = trade_store.list_opportunities(limit=500)

    buckets = {"<0%": 0, "0-1%": 0, "1-2%": 0, "2-5%": 0, "5-10%": 0, ">10%": 0}
    resolution_counts = {"MATCHED": 0, "DIFFERS": 0, "UNVERIFIED": 0}
    for opportunity in current:
        margin = float(opportunity.get("net_margin") or 0)
        if margin < 0:
            buckets["<0%"] += 1
        elif margin < 0.01:
            buckets["0-1%"] += 1
        elif margin < 0.02:
            buckets["1-2%"] += 1
        elif margin < 0.05:
            buckets["2-5%"] += 1
        elif margin < 0.10:
            buckets["5-10%"] += 1
        else:
            buckets[">10%"] += 1
        status = opportunity.get("resolution_status", "UNVERIFIED")
        resolution_counts[status] = resolution_counts.get(status, 0) + 1

    total = sum(resolution_counts.values()) or 1
    return {
        "scans": scans[:50],
        "scan_count": len(scans),
        "pnl": pnl,
        "spread_distribution": buckets,
        "resolution_counts": resolution_counts,
        "resolution_failure_rate": round(
            (resolution_counts.get("DIFFERS", 0) + resolution_counts.get("UNVERIFIED", 0))
            / total,
            4,
        ),
        "state": store.load_state(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/dashboard")
async def dashboard() -> dict[str, Any]:
    """Everything the dashboard needs, in one round trip.

    The dashboard refreshes every 30 seconds; four separate calls each time
    would be four times the cold-start risk for no benefit.
    """
    settings: Settings = store.load_settings()
    state = store.load_state()

    balances = await exposure_module.fetch_balances(pmxt_client, settings)
    report = exposure_module.exposure_report(balances, settings)
    pnl = trade_store.pnl_summary(days=7)

    last_scan = state.get("last_scan_at")
    next_scan = None
    if last_scan:
        try:
            next_scan = (
                datetime.fromisoformat(last_scan).timestamp()
                + settings.scanning.poll_interval_seconds
            )
        except ValueError:
            next_scan = None

    return {
        "opportunities": trade_store.list_opportunities(limit=50),
        "open_trades": trade_store.open_trades(),
        "pnl": pnl,
        "exposure": report,
        "balances": balances,
        "state": state,
        "status": {
            "trading_enabled": settings.execution.trading_enabled,
            "dry_run": settings.execution.dry_run,
            "kill_switch": settings.risk.kill_switch,
            "kill_switch_reason": settings.risk.kill_switch_reason,
            "stake_per_trade": settings.effective_stake(),
            "min_margin_to_trade": settings.margin.min_margin_to_trade,
            "required_resolution_status": settings.risk.required_resolution_status,
            "enabled_venues": settings.enabled_venues(),
            "last_scan_at": last_scan,
            "last_scan_id": state.get("last_scan_id"),
            "next_scan_at": next_scan,
            "poll_interval_seconds": settings.scanning.poll_interval_seconds,
        },
        "alerts": _alerts(settings, state, pnl),
    }


def _alerts(settings: Settings, state: dict[str, Any], pnl: dict[str, Any]) -> list[dict[str, str]]:
    """Conditions that need Charles's attention, most severe first."""
    alerts: list[dict[str, str]] = []
    totals = pnl.get("totals", {})

    exposed = totals.get("exposed", 0)
    if exposed:
        alerts.append(
            {
                "level": "critical",
                "message": f"{exposed} trade(s) EXPOSED — an unwind failed and a position is unhedged",
            }
        )

    failed = int(state.get("failed_unwinds_today") or 0)
    if failed >= settings.alerts.failed_unwinds_threshold and failed:
        alerts.append(
            {"level": "critical", "message": f"{failed} failed unwind(s) today"}
        )

    unmatched = int(state.get("unmatched_legs_today") or 0)
    if unmatched >= settings.alerts.unmatched_legs_threshold and unmatched:
        alerts.append(
            {
                "level": "warning",
                "message": f"{unmatched} unmatched leg event(s) today — the execution layer needed containment",
            }
        )

    if settings.risk.kill_switch:
        alerts.append(
            {
                "level": "warning",
                "message": f"kill switch engaged: {settings.risk.kill_switch_reason or 'no reason recorded'}",
            }
        )

    errors = int(state.get("errors_today") or 0)
    if errors >= settings.alerts.errors_threshold and errors:
        alerts.append({"level": "warning", "message": f"{errors} errors today"})

    if settings.risk.allow_unverified_override:
        alerts.append(
            {
                "level": "warning",
                "message": "UNVERIFIED resolution override is enabled — pairs that could not be confirmed equivalent are tradeable",
            }
        )

    pnl_today = float(state.get("realised_pnl_today") or 0)
    if pnl_today <= -abs(settings.risk.daily_loss_limit):
        alerts.append(
            {"level": "critical", "message": f"daily loss limit breached ({pnl_today:.2f})"}
        )

    return alerts


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT)
