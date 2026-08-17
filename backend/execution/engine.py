"""Execution orchestration.

Ties the pre-trade checks to the leg coordinator and the trade record. One
opportunity in, one persisted trade out, with every decision written down.

Dry run does exactly what live does up to the point of submission, then
stops. It does not invent fills — the legs are recorded as not submitted, and
the trade carries `dry_run: true`. A dry run that fabricated a fill would
tell Charles the execution layer works when it has never been exercised.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from execution import legs as leg_engine
from execution import unwind as unwind_module
from logging_setup import log
from models import (
    LegStatus,
    Opportunity,
    Trade,
    TradeStatus,
)
from risk import limits
from settings import secrets
from settings.schema import Settings
from settings.store import bump_state
from storage import trades as trade_store
from venues.adapters import VenueAdapter
from venues.pmxt_client import PmxtClient

logger = logging.getLogger(__name__)


async def build_adapters(
    client: PmxtClient, venues: list[str]
) -> dict[str, VenueAdapter]:
    """Credentialed adapters for the venues a trade needs.

    Credentials are read from Secret Manager here and live only as long as
    the adapter does.
    """
    adapters: dict[str, VenueAdapter] = {}
    for venue in venues:
        credentials = secrets.load_credentials(venue)
        if not credentials:
            continue
        adapters[venue] = VenueAdapter(venue, client, credentials)
    return adapters


def _trade_from_opportunity(
    opportunity: Opportunity, settings: Settings, shares: float
) -> Trade:
    assert opportunity.leg_a and opportunity.leg_b
    trade = Trade(
        opportunity_id=opportunity.id,
        scan_id=opportunity.scan_id,
        intended_shares=round(shares, 6),
        intended_cost=round(
            opportunity.leg_a.total_cost + opportunity.leg_b.total_cost, 6
        ),
        expected_profit=opportunity.profit,
        expected_margin=opportunity.net_margin,
        resolution_status=opportunity.resolution.status.value,
        resolution_override=(
            opportunity.resolution.status.value != "MATCHED"
            and settings.risk.allow_unverified_override
        ),
        dry_run=settings.execution.dry_run,
        legs=[
            leg_engine.build_leg(opportunity.leg_a, shares),
            leg_engine.build_leg(opportunity.leg_b, shares),
        ],
    )
    trade.record(
        "created",
        opportunity=opportunity.id,
        expected_margin=opportunity.net_margin,
        resolution_status=trade.resolution_status,
        venues=opportunity.venues,
    )
    if trade.resolution_override:
        # An override is a deliberate decision to trade a pair we could not
        # confirm is the same question. It is logged where it will be found.
        log(
            logger,
            logging.WARNING,
            "trading a non-MATCHED pair under explicit override",
            trade_id=trade.id,
            resolution_status=trade.resolution_status,
            reason=settings.risk.override_reason,
        )
        trade.record(
            "resolution_override",
            status=trade.resolution_status,
            reason=settings.risk.override_reason,
        )
    return trade


async def execute_opportunity(
    opportunity: Opportunity,
    client: PmxtClient,
    settings: Settings,
    context: limits.RiskContext,
) -> Optional[Trade]:
    """Run one opportunity through checks and execution.

    Returns the persisted trade, or None if the pre-trade checks refused it.
    """
    decision = limits.evaluate(opportunity, context)
    if not decision.allowed:
        opportunity.tradeable = False
        opportunity.blocked_reasons = decision.reasons
        return None

    shares = opportunity.shares
    if shares <= 0:
        return None

    trade = _trade_from_opportunity(opportunity, settings, shares)
    trade_store.save_trade(trade)

    if settings.execution.dry_run:
        for leg in trade.legs:
            leg.status = LegStatus.UNFILLED
            leg.error = "dry run — order not submitted"
        trade.status = TradeStatus.CANCELLED
        trade.settlement_notes = "dry run: pre-trade checks passed, nothing submitted"
        trade.record("dry_run", reason="execution.dry_run is enabled")
        trade_store.save_trade(trade)
        log(
            logger,
            logging.INFO,
            "dry run: checks passed, no order submitted",
            trade_id=trade.id,
            expected_margin=opportunity.net_margin,
        )
        return trade

    adapters = await build_adapters(client, opportunity.venues)
    missing = [v for v in opportunity.venues if v not in adapters]
    if missing:
        trade.status = TradeStatus.FAILED
        trade.settlement_notes = f"no usable credentials for {missing}"
        trade.record("aborted", reason="missing credentials", venues=missing)
        trade_store.save_trade(trade)
        return trade

    try:
        outcome = await leg_engine.execute_pair(trade, adapters, settings, shares)
    except Exception as exc:  # noqa: BLE001 - execution must always record
        trade.status = TradeStatus.EXPOSED
        trade.settlement_notes = (
            f"execution raised {type(exc).__name__}: {exc}. "
            "Position state must be verified manually."
        )
        trade.record("execution_error", error=str(exc))
        trade_store.save_trade(trade)
        log(
            logger,
            logging.ERROR,
            "execution raised — trade state uncertain",
            trade_id=trade.id,
            error=str(exc),
        )
        bump_state(errors_today=1)
        raise

    trade_store.save_trade(trade)

    # Counters. Unmatched legs are tracked daily and cumulatively because
    # that number, not P&L, says whether the execution layer works.
    deltas: dict[str, float] = {"trades_today": 1}
    if outcome.unmatched:
        deltas["unmatched_legs_today"] = 1
        deltas["unmatched_legs_total"] = 1
    if outcome.exposed:
        deltas["failed_unwinds_today"] = 1
        deltas["failed_unwinds_total"] = 1
    if trade.realised_pnl:
        deltas["realised_pnl_today"] = trade.realised_pnl
    bump_state(**deltas)

    # Keep the in-scan view current so the next opportunity in this cycle
    # sees the capital this one just committed.
    context.open_trade_count += 1
    context.trades_today += 1
    if outcome.unmatched:
        context.unmatched_legs_today += 1
    for leg in trade.legs:
        if leg.filled_shares > 0:
            context.exposure_by_venue[leg.venue] = (
                context.exposure_by_venue.get(leg.venue, 0.0) + leg.cost
            )

    return trade


async def manual_unwind(
    trade_id: str, client: PmxtClient, settings: Settings, actor: str = "dashboard"
) -> dict[str, Any]:
    """Close every filled leg of a trade at market, on request.

    The dashboard's escape hatch. Used when a trade is EXPOSED and the
    automatic unwind failed, or when Charles wants out of a hedged position
    before resolution.
    """
    raw = trade_store.get_trade(trade_id)
    if not raw:
        return {"ok": False, "error": f"trade {trade_id} not found"}

    trade = _rehydrate(raw)
    filled = [leg for leg in trade.legs if leg.filled_shares > 0 and leg.status not in {LegStatus.UNWOUND}]
    if not filled:
        return {"ok": False, "error": "trade has no open legs to unwind"}

    adapters = await build_adapters(client, [leg.venue for leg in filled])
    trade.record("manual_unwind_requested", actor=actor)

    results: list[dict[str, Any]] = []
    total_cost = 0.0
    all_succeeded = True
    for leg in filled:
        adapter = adapters.get(leg.venue)
        if not adapter:
            all_succeeded = False
            results.append(
                {"venue": leg.venue, "ok": False, "error": "no usable credentials"}
            )
            continue
        result = await unwind_module.unwind_leg(adapter, leg, settings, trade=trade)
        total_cost += result.cost
        all_succeeded = all_succeeded and result.succeeded
        results.append(
            {
                "venue": leg.venue,
                "ok": result.succeeded,
                "shares": result.shares_unwound,
                "proceeds": result.proceeds,
                "cost": result.cost,
                "error": result.error,
            }
        )

    trade.unwind_attempted = True
    trade.unwind_succeeded = all_succeeded
    trade.status = TradeStatus.SETTLED if all_succeeded else TradeStatus.EXPOSED
    trade.realised_pnl = round(-total_cost, 6)
    trade.settlement_notes = (
        f"manually unwound by {actor}"
        if all_succeeded
        else f"manual unwind incomplete — see legs. Requested by {actor}."
    )
    trade.record("manual_unwind_completed", actor=actor, succeeded=all_succeeded)
    trade_store.save_trade(trade)

    return {
        "ok": all_succeeded,
        "trade_id": trade_id,
        "status": trade.status.value,
        "legs": results,
        "realised_pnl": trade.realised_pnl,
    }


def _rehydrate(raw: dict[str, Any]) -> Trade:
    """Rebuild a Trade from its Firestore document."""
    from models import TradeLeg

    trade = Trade(
        id=raw.get("id", ""),
        opportunity_id=raw.get("opportunity_id", ""),
        scan_id=raw.get("scan_id", ""),
        status=TradeStatus(raw.get("status", "PENDING")),
        intended_shares=float(raw.get("intended_shares") or 0),
        intended_cost=float(raw.get("intended_cost") or 0),
        expected_profit=float(raw.get("expected_profit") or 0),
        expected_margin=float(raw.get("expected_margin") or 0),
        actual_cost=float(raw.get("actual_cost") or 0),
        realised_pnl=raw.get("realised_pnl"),
        settlement_notes=raw.get("settlement_notes", ""),
        unmatched_leg=bool(raw.get("unmatched_leg")),
        containment_cost=raw.get("containment_cost"),
        unwind_attempted=bool(raw.get("unwind_attempted")),
        unwind_succeeded=raw.get("unwind_succeeded"),
        resolution_status=raw.get("resolution_status", "UNVERIFIED"),
        resolution_override=bool(raw.get("resolution_override")),
        dry_run=bool(raw.get("dry_run")),
        events=list(raw.get("events") or []),
    )
    for item in raw.get("legs") or []:
        leg = TradeLeg(
            venue=item.get("venue", ""),
            market_id=item.get("market_id", ""),
            outcome_id=item.get("outcome_id", ""),
            outcome_label=item.get("outcome_label", ""),
            market_title=item.get("market_title", ""),
            side=item.get("side", "buy"),
            intended_shares=float(item.get("intended_shares") or 0),
            intended_price=float(item.get("intended_price") or 0),
            order_id=item.get("order_id"),
            status=LegStatus(item.get("status", "PENDING")),
            filled_shares=float(item.get("filled_shares") or 0),
            avg_fill_price=float(item.get("avg_fill_price") or 0),
            fee=float(item.get("fee") or 0),
            cost=float(item.get("cost") or 0),
            attempts=int(item.get("attempts") or 0),
            error=item.get("error"),
        )
        trade.legs.append(leg)
    return trade
