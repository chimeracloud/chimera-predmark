"""Unwinding a leg that should not be held.

Called when one leg filled and the other did not. What is left is a naked
directional position — the exact outcome this strategy exists to avoid — and
it has to go, now.

The governing judgement: a bad price is cheaper than an unhedged position.
Unwind sells at market and does not haggle. `unwind_max_loss` does not gate
the sale; it decides whether the result gets flagged as a bad unwind for
Charles to look at. A setting that could refuse to close an unhedged position
would be a setting that turns a small loss into an open-ended one.

Failure to unwind is recorded as EXPOSED and surfaced prominently. That state
needs a human, and pretending otherwise helps nobody.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from logging_setup import log
from models import LegStatus, Trade, TradeLeg
from settings.schema import Settings
from venues.adapters import VenueAdapter
from venues.pmxt_client import PmxtError

logger = logging.getLogger(__name__)


@dataclass
class UnwindResult:
    succeeded: bool
    shares_unwound: float = 0.0
    proceeds: float = 0.0
    cost: float = 0.0  # realised loss: what we paid minus what we recovered
    attempts: int = 0
    error: Optional[str] = None
    flagged_bad_price: bool = False


async def unwind_leg(
    adapter: VenueAdapter,
    leg: TradeLeg,
    settings: Settings,
    trade: Optional[Trade] = None,
    shares: Optional[float] = None,
) -> UnwindResult:
    """Sell back a filled leg at market.

    Retries because a failed unwind leaves real exposure — this is the one
    place in the codebase where retrying an order is right, since the
    position we are closing is known to exist and selling it twice is
    prevented by the venue having nothing left to sell.
    """
    target = shares if shares is not None else leg.filled_shares
    if target <= 0:
        return UnwindResult(succeeded=True, shares_unwound=0.0)

    entry_cost = target * leg.avg_fill_price
    attempts = 0
    proceeds = 0.0
    unwound = 0.0
    last_error: Optional[str] = None

    log(
        logger,
        logging.WARNING,
        "UNWINDING unhedged leg",
        venue=leg.venue,
        outcome_id=leg.outcome_id,
        shares=target,
        entry_price=leg.avg_fill_price,
    )
    if trade:
        trade.record(
            "unwind_started",
            venue=leg.venue,
            shares=target,
            entry_price=leg.avg_fill_price,
        )

    remaining = target
    while attempts <= settings.execution.unwind_retry_limit and remaining > 1e-9:
        attempts += 1
        try:
            order = await adapter.create_order(
                market_id=leg.market_id,
                outcome_id=leg.outcome_id,
                side="sell",
                shares=remaining,
                order_type="market",
                slippage_pct=max(
                    settings.venues[leg.venue].slippage_pct
                    if leg.venue in settings.venues
                    else 2.0,
                    # Widen slippage on each retry: getting out matters more
                    # than the price we get out at.
                    attempts * 5.0,
                ),
            )
        except PmxtError as exc:
            last_error = f"{exc.code}: {exc}"
            log(
                logger,
                logging.ERROR,
                "unwind attempt failed",
                venue=leg.venue,
                attempt=attempts,
                code=exc.code,
            )
            await asyncio.sleep(min(2.0 * attempts, 5.0))
            continue

        filled = float(order.get("filled_shares") or 0.0)
        price = float(order.get("price") or 0.0)
        if filled > 0:
            unwound += filled
            proceeds += filled * price
            remaining -= filled
            leg.unwind_order_id = str(order.get("id") or "")
            leg.unwind_shares = round(unwound, 6)
            leg.unwind_proceeds = round(proceeds, 6)
        else:
            last_error = f"unwind order {order.get('id')} did not fill"
            await asyncio.sleep(1.0)

    realised_cost = round(entry_cost - proceeds, 6)
    succeeded = remaining <= 1e-9 and unwound > 0

    if succeeded:
        leg.status = LegStatus.UNWOUND
        loss_fraction = (
            realised_cost / entry_cost if entry_cost > 0 else 0.0
        )
        flagged = loss_fraction > settings.execution.unwind_max_loss
        log(
            logger,
            logging.WARNING,
            "leg unwound",
            venue=leg.venue,
            shares=round(unwound, 6),
            realised_cost=realised_cost,
            loss_fraction=round(loss_fraction, 4),
            worse_than_tolerance=flagged,
        )
        if trade:
            trade.record(
                "unwind_completed",
                venue=leg.venue,
                shares=round(unwound, 6),
                proceeds=round(proceeds, 6),
                realised_cost=realised_cost,
                flagged_bad_price=flagged,
            )
        return UnwindResult(
            succeeded=True,
            shares_unwound=round(unwound, 6),
            proceeds=round(proceeds, 6),
            cost=realised_cost,
            attempts=attempts,
            flagged_bad_price=flagged,
        )

    leg.status = LegStatus.UNWIND_FAILED
    leg.error = last_error
    log(
        logger,
        logging.ERROR,
        "UNWIND FAILED — POSITION IS EXPOSED AND NEEDS A HUMAN",
        venue=leg.venue,
        outcome_id=leg.outcome_id,
        market=leg.market_title,
        shares_remaining=round(remaining, 6),
        attempts=attempts,
        error=last_error,
    )
    if trade:
        trade.record(
            "unwind_failed",
            venue=leg.venue,
            shares_remaining=round(remaining, 6),
            attempts=attempts,
            error=last_error,
        )
    return UnwindResult(
        succeeded=False,
        shares_unwound=round(unwound, 6),
        proceeds=round(proceeds, 6),
        cost=realised_cost,
        attempts=attempts,
        error=last_error or "unwind did not complete",
    )
