"""Trades and opportunities in Firestore.

Trades are written at every state transition, not just at the end. If the
container dies between placing leg A and placing leg B, the record of leg A
already exists and the exposure is visible on the dashboard — which is the
difference between a problem you can see and money you have lost track of.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import config
from logging_setup import log
from models import Trade, TradeStatus
from settings.store import client

logger = logging.getLogger(__name__)


def _trades():
    return client().collection(config.TRADES_COLLECTION)


def _opportunities():
    return client().collection(config.OPPORTUNITIES_COLLECTION)


# --- trades ---------------------------------------------------------------


def save_trade(trade: Trade) -> None:
    """Persist a trade. Called at every transition."""
    try:
        _trades().document(trade.id).set(trade.to_dict())
    except Exception as exc:  # noqa: BLE001
        # A trade we cannot record is a trade we cannot manage. Log loudly.
        log(
            logger,
            logging.ERROR,
            "TRADE PERSISTENCE FAILED",
            trade_id=trade.id,
            status=trade.status.value,
            error=str(exc),
        )


def get_trade(trade_id: str) -> Optional[dict[str, Any]]:
    try:
        snapshot = _trades().document(trade_id).get()
        return snapshot.to_dict() if snapshot.exists else None
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "trade read failed", trade_id=trade_id, error=str(exc))
        return None


def list_trades(
    status: Optional[str] = None,
    limit: int = 200,
    since: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    from google.cloud import firestore

    try:
        query = _trades().order_by("created_at", direction=firestore.Query.DESCENDING)
        if status:
            query = _trades().where("status", "==", status).order_by(
                "created_at", direction=firestore.Query.DESCENDING
            )
        docs = [doc.to_dict() for doc in query.limit(limit).stream()]
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "trade listing failed", error=str(exc))
        return []

    if since:
        cutoff = since.isoformat()
        docs = [d for d in docs if (d.get("created_at") or "") >= cutoff]
    return docs


def open_trades() -> list[dict[str, Any]]:
    """Trades holding capital: filled and awaiting resolution, or exposed."""
    results: list[dict[str, Any]] = []
    for status in (TradeStatus.OPEN.value, TradeStatus.EXPOSED.value, TradeStatus.PENDING.value):
        results.extend(list_trades(status=status, limit=200))
    return results


def count_open_trades() -> int:
    return len(
        [
            t
            for t in open_trades()
            if t.get("status") in {TradeStatus.OPEN.value, TradeStatus.PENDING.value}
        ]
    )


# --- P&L ------------------------------------------------------------------


def pnl_summary(days: int = 30) -> dict[str, Any]:
    """P&L per day and per venue, plus the execution-quality counters.

    Unmatched legs are surfaced alongside P&L deliberately. A profitable week
    with three unmatched legs means the execution layer is broken and got
    lucky.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    trades = list_trades(limit=1000, since=since)

    per_day: dict[str, dict[str, float]] = {}
    per_venue: dict[str, dict[str, float]] = {}
    totals = {
        "trades": 0,
        "realised_pnl": 0.0,
        "expected_profit": 0.0,
        "capital_deployed": 0.0,
        "unmatched_legs": 0,
        "failed_unwinds": 0,
        "containment_cost": 0.0,
        "open": 0,
        "settled": 0,
        "exposed": 0,
    }

    for trade in trades:
        day = (trade.get("created_at") or "")[:10]
        pnl = trade.get("realised_pnl")
        pnl = float(pnl) if pnl is not None else 0.0
        cost = float(trade.get("actual_cost") or 0.0)

        totals["trades"] += 1
        totals["realised_pnl"] += pnl
        totals["expected_profit"] += float(trade.get("expected_profit") or 0.0)
        totals["capital_deployed"] += cost

        status = trade.get("status")
        if status == TradeStatus.OPEN.value:
            totals["open"] += 1
        elif status == TradeStatus.SETTLED.value:
            totals["settled"] += 1
        elif status == TradeStatus.EXPOSED.value:
            totals["exposed"] += 1

        if trade.get("unmatched_leg"):
            totals["unmatched_legs"] += 1
            containment = trade.get("containment_cost")
            if containment is not None:
                totals["containment_cost"] += float(containment)
        if trade.get("unwind_attempted") and trade.get("unwind_succeeded") is False:
            totals["failed_unwinds"] += 1

        bucket = per_day.setdefault(
            day, {"realised_pnl": 0.0, "trades": 0, "capital": 0.0, "unmatched": 0}
        )
        bucket["realised_pnl"] += pnl
        bucket["trades"] += 1
        bucket["capital"] += cost
        bucket["unmatched"] += 1 if trade.get("unmatched_leg") else 0

        for leg in trade.get("legs") or []:
            venue = leg.get("venue") or "unknown"
            venue_bucket = per_venue.setdefault(
                venue, {"legs": 0, "filled": 0.0, "cost": 0.0, "fees": 0.0}
            )
            venue_bucket["legs"] += 1
            venue_bucket["filled"] += float(leg.get("filled_shares") or 0.0)
            venue_bucket["cost"] += float(leg.get("cost") or 0.0)
            venue_bucket["fees"] += float(leg.get("fee") or 0.0)

    today = date.today().isoformat()
    return {
        "totals": {k: round(v, 6) if isinstance(v, float) else v for k, v in totals.items()},
        "today": per_day.get(
            today, {"realised_pnl": 0.0, "trades": 0, "capital": 0.0, "unmatched": 0}
        ),
        "per_day": dict(sorted(per_day.items(), reverse=True)),
        "per_venue": per_venue,
        "window_days": days,
    }


def realised_pnl_today() -> float:
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    total = 0.0
    for trade in list_trades(limit=500, since=today):
        pnl = trade.get("realised_pnl")
        if pnl is not None:
            total += float(pnl)
        # An unwound single leg costs money the moment it happens, whether or
        # not the trade has otherwise settled.
        containment = trade.get("containment_cost")
        if containment is not None and pnl is None:
            total -= abs(float(containment))
    return round(total, 6)


def exposure_by_venue() -> dict[str, float]:
    """Capital currently committed per venue.

    Positions cannot be netted across platforms — a YES on one venue and a NO
    on another are two separate holdings with two separate collateral
    requirements. So exposure is the sum of both legs, never the difference.
    """
    exposure: dict[str, float] = {}
    for trade in open_trades():
        for leg in trade.get("legs") or []:
            if float(leg.get("filled_shares") or 0) <= 0:
                continue
            venue = leg.get("venue") or "unknown"
            exposure[venue] = exposure.get(venue, 0.0) + float(leg.get("cost") or 0.0)
    return {k: round(v, 6) for k, v in exposure.items()}


# --- opportunities --------------------------------------------------------


def replace_opportunities(scan_id: str, opportunities: list[dict[str, Any]]) -> None:
    """Swap in the current scan's opportunities.

    Opportunities are a live view, not a ledger — the durable record of every
    scan is in GCS. Old documents are removed so the dashboard cannot show a
    spread that closed twenty minutes ago as though it were live.
    """
    try:
        batch = client().batch()
        stale = list(_opportunities().limit(500).stream())
        for doc in stale:
            batch.delete(doc.reference)
        for opportunity in opportunities[:200]:
            batch.set(_opportunities().document(opportunity["id"]), opportunity)
        batch.commit()
    except Exception as exc:  # noqa: BLE001
        log(
            logger,
            logging.ERROR,
            "opportunity write failed",
            scan_id=scan_id,
            error=str(exc),
        )


def list_opportunities(
    limit: int = 100,
    tradeable_only: bool = False,
    min_margin: Optional[float] = None,
    resolution_status: Optional[str] = None,
) -> list[dict[str, Any]]:
    try:
        docs = [doc.to_dict() for doc in _opportunities().limit(500).stream()]
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "opportunity read failed", error=str(exc))
        return []

    if tradeable_only:
        docs = [d for d in docs if d.get("tradeable")]
    if min_margin is not None:
        docs = [d for d in docs if float(d.get("net_margin") or 0) >= min_margin]
    if resolution_status:
        docs = [d for d in docs if d.get("resolution_status") == resolution_status]

    docs.sort(key=lambda d: float(d.get("net_margin") or 0), reverse=True)
    return docs[:limit]
