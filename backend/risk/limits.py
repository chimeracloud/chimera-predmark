"""Pre-trade checks.

Every check in section 2.2 of the brief, in one place, evaluated for every
trade. The contract is deliberately blunt: `evaluate` returns a list of
reasons the trade must not happen, and an empty list is the only thing that
permits execution. There is no "warn and proceed".

Checks are ordered cheapest and most decisive first — the kill switch is read
before anything else and short-circuits the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from logging_setup import log
from models import Opportunity, ResolutionStatus
from settings.schema import Settings

logger = logging.getLogger(__name__)


@dataclass
class RiskContext:
    """Everything the checks need, gathered once per scan."""

    settings: Settings
    killed: bool = False
    open_trade_count: int = 0
    trades_today: int = 0
    realised_pnl_today: float = 0.0
    unmatched_legs_today: int = 0
    exposure_by_venue: dict[str, float] = field(default_factory=dict)
    balances: dict[str, dict[str, float]] = field(default_factory=dict)
    credentialed_venues: set[str] = field(default_factory=set)

    @property
    def total_exposure(self) -> float:
        return sum(self.exposure_by_venue.values())


@dataclass
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    # Size the checks would permit, which may be below what was requested.
    permitted_stake: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "permitted_stake": self.permitted_stake,
        }


def evaluate(opportunity: Opportunity, context: RiskContext) -> Decision:
    """Decide whether one opportunity may be executed, and at what size."""
    settings = context.settings
    reasons: list[str] = []

    # 1. Kill switch. Read fresh from Firestore by the caller.
    if context.killed or settings.risk.kill_switch:
        reason = settings.risk.kill_switch_reason or "no reason recorded"
        return Decision(False, [f"kill switch engaged ({reason})"], 0.0)

    # 2. Trading enabled at all.
    if not settings.execution.trading_enabled:
        return Decision(False, ["trading is disabled in settings"], 0.0)

    # 3. Resolution status. The pair must be MATCHED unless Charles has
    # explicitly overridden, and the override is recorded on the trade.
    status = opportunity.resolution.status
    if status is ResolutionStatus.DIFFERS:
        reasons.append("resolution criteria differ between venues")
    elif status is ResolutionStatus.UNVERIFIED:
        override = (
            settings.risk.allow_unverified_override
            and settings.risk.required_resolution_status == "MATCHED_OR_UNVERIFIED"
        )
        if not override:
            reasons.append(
                "resolution equivalence is UNVERIFIED and override is not enabled"
            )

    # 4. Margin floor, net of both venues' fees.
    if opportunity.net_margin < settings.margin.min_margin_to_trade:
        reasons.append(
            f"net margin {opportunity.net_margin:.4f} below floor "
            f"{settings.margin.min_margin_to_trade:.4f}"
        )

    # 4b. And a ceiling. Real cross-venue spreads are a few per cent; a
    # margin far above that is a stale quote, a mis-mapped outcome, or a
    # venue serving the wrong book. Observed live on Limitless, which
    # returned the same order book for both outcomes and so presented a
    # 1090% "opportunity". The check lives here rather than only in the scan
    # because this function is the authority on what may be traded.
    if opportunity.net_margin > settings.margin.max_plausible_margin:
        reasons.append(
            f"net margin {opportunity.net_margin:.2%} exceeds the plausibility "
            f"ceiling {settings.margin.max_plausible_margin:.0%} — treated as a "
            "data error, not an opportunity"
        )

    # 5. Both legs present and priced.
    if not opportunity.leg_a or not opportunity.leg_b:
        return Decision(False, reasons + ["opportunity is missing a leg"], 0.0)

    # 6. Venues enabled and credentialed. A venue we cannot authenticate to
    # is a venue whose leg will fail — and a failed second leg is precisely
    # the unhedged position this whole design exists to prevent.
    for leg in (opportunity.leg_a, opportunity.leg_b):
        venue_settings = settings.venues.get(leg.venue)
        if not venue_settings or not venue_settings.enabled:
            reasons.append(f"venue {leg.venue} is not enabled")
        if leg.venue not in context.credentialed_venues:
            reasons.append(f"venue {leg.venue} has no usable credentials")

    # 7. Depth. Both books must actually hold the size.
    stake = settings.effective_stake()
    for leg in (opportunity.leg_a, opportunity.leg_b):
        if settings.execution.require_full_depth and leg.depth_available < opportunity.shares:
            reasons.append(
                f"{leg.venue} book holds {leg.depth_available:.1f} shares, "
                f"needs {opportunity.shares:.1f}"
            )
        if leg.depth_source == "none":
            reasons.append(f"no depth information for {leg.venue}")

    # 8. Price sanity. The tails are where stale quotes live.
    for leg in (opportunity.leg_a, opportunity.leg_b):
        if not (
            settings.margin.min_outcome_price
            <= leg.avg_price
            <= settings.margin.max_outcome_price
        ):
            reasons.append(
                f"{leg.venue} price {leg.avg_price:.4f} outside "
                f"[{settings.margin.min_outcome_price}, "
                f"{settings.margin.max_outcome_price}]"
            )

    # 9. Balances. Capital must sit on both venues simultaneously — a
    # position on one cannot collateralise a position on the other.
    for leg in (opportunity.leg_a, opportunity.leg_b):
        balance = context.balances.get(leg.venue)
        if balance is None:
            reasons.append(f"balance unknown for {leg.venue}")
        elif balance.get("available", 0.0) < leg.total_cost:
            reasons.append(
                f"{leg.venue} available balance {balance.get('available', 0):.2f} "
                f"below leg cost {leg.total_cost:.2f}"
            )

    # 10. Concurrency and per-day caps.
    if context.open_trade_count >= settings.execution.max_concurrent_trades:
        reasons.append(
            f"{context.open_trade_count} trades already open, limit is "
            f"{settings.execution.max_concurrent_trades}"
        )
    if context.trades_today >= settings.risk.daily_trade_limit:
        reasons.append(
            f"daily trade limit reached ({settings.risk.daily_trade_limit})"
        )

    # 11. Daily loss limit.
    if context.realised_pnl_today <= -abs(settings.risk.daily_loss_limit):
        reasons.append(
            f"daily loss limit breached ({context.realised_pnl_today:.2f} vs "
            f"-{settings.risk.daily_loss_limit:.2f})"
        )

    # 12. Exposure caps, per venue and in total. Summed, never netted.
    for leg in (opportunity.leg_a, opportunity.leg_b):
        venue_settings = settings.venues.get(leg.venue)
        if not venue_settings:
            continue
        projected = context.exposure_by_venue.get(leg.venue, 0.0) + leg.total_cost
        if projected > venue_settings.max_exposure:
            reasons.append(
                f"{leg.venue} exposure would reach {projected:.2f}, cap is "
                f"{venue_settings.max_exposure:.2f}"
            )
    projected_total = context.total_exposure + opportunity.total_cost
    if projected_total > settings.risk.max_total_exposure:
        reasons.append(
            f"total exposure would reach {projected_total:.2f}, cap is "
            f"{settings.risk.max_total_exposure:.2f}"
        )

    # 13. Automatic halt when the execution layer is misbehaving.
    halt_at = settings.alerts.halt_on_unmatched_legs
    if halt_at and context.unmatched_legs_today >= halt_at:
        reasons.append(
            f"{context.unmatched_legs_today} unmatched legs today, at or above "
            f"the halt threshold of {halt_at}"
        )

    allowed = not reasons
    if not allowed:
        log(
            logger,
            logging.INFO,
            "trade blocked by pre-trade checks",
            opportunity=opportunity.id,
            reasons=reasons,
        )
    return Decision(allowed, reasons, stake if allowed else 0.0)
