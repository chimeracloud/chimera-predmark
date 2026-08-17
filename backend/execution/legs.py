"""Both legs, or neither.

This is the module the whole system is judged on. A filled leg with an
unfilled counter-leg is not an arbitrage — it is an unhedged directional
position, which is the one outcome this strategy exists to avoid.

The sequence:

  1. Submit both legs concurrently. Not sequentially: the gap between two
     sequential submissions is the window in which the second price moves.
  2. Poll both for fills, in seconds.
  3. Compare filled sizes. The hedged quantity is min(a, b); anything above
     that on either side is naked.
  4. If a leg is short, chase it — resubmit for the shortfall at a worse
     price, up to the configured retry limit and reprice ceiling.
  5. If it is still short, unwind the excess on the over-filled side.
  6. Record the whole thing as a containment failure with its realised cost.

Partial fills on both sides are handled the same way as a clean single-leg
fill, because they are the same problem: the hedge covers min(a, b) and the
difference is exposure. Treating only the all-or-nothing case would leave the
commonest real failure unhandled.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from execution import orders, unwind
from logging_setup import log
from models import LegQuote, LegStatus, Trade, TradeLeg, TradeStatus
from settings.schema import Settings
from venues.adapters import VenueAdapter

logger = logging.getLogger(__name__)


@dataclass
class ExecutionOutcome:
    trade: Trade
    hedged_shares: float = 0.0
    unmatched: bool = False
    containment_cost: Optional[float] = None
    exposed: bool = False
    notes: list[str] = field(default_factory=list)


def build_leg(quote: LegQuote, shares: float) -> TradeLeg:
    return TradeLeg(
        venue=quote.venue,
        market_id=quote.market_id,
        outcome_id=quote.outcome_id,
        outcome_label=quote.outcome_label,
        market_title=quote.market_title,
        side="buy",
        intended_shares=round(shares, 6),
        intended_price=quote.avg_price,
    )


async def _submit_and_track(
    adapter: VenueAdapter,
    leg: TradeLeg,
    shares: float,
    settings: Settings,
    trade: Trade,
) -> None:
    """Submit one leg and resolve it to a known fill state."""
    venue_settings = settings.venues.get(leg.venue)
    order_type = venue_settings.order_type if venue_settings else "market"
    slippage = venue_settings.slippage_pct if venue_settings else 2.0

    # A limit order at the price we quoted risks resting unfilled, which is
    # how a hedge becomes a naked position. Where limit is configured, price
    # it through the book by the tolerance so it behaves like a taker.
    price = None
    if order_type == "limit":
        price = round(leg.intended_price + settings.execution.slippage_tolerance, 4)

    leg.submitted_at = trade.updated_at
    result = await orders.submit(
        adapter, leg, shares, order_type=order_type, price=price, slippage_pct=slippage
    )
    leg.status = LegStatus.SUBMITTED

    if not result.ok:
        leg.error = result.error
        if result.ambiguous:
            # We do not know whether the venue took it. Ask the venue.
            trade.record("leg_ambiguous", venue=leg.venue, error=result.error)
            position = await orders.reconcile_ambiguous(adapter, leg)
            if position:
                leg.filled_shares = abs(float(position.get("size") or 0.0))
                leg.avg_fill_price = float(
                    position.get("entry_price") or leg.intended_price
                )
                leg.cost = round(leg.filled_shares * leg.avg_fill_price, 6)
                leg.status = (
                    LegStatus.FILLED
                    if leg.filled_shares >= leg.intended_shares - 1e-9
                    else LegStatus.PARTIAL
                )
                trade.record(
                    "leg_reconciled", venue=leg.venue, filled=leg.filled_shares
                )
                return
        leg.status = LegStatus.REJECTED if not result.ambiguous else LegStatus.UNFILLED
        trade.record("leg_failed", venue=leg.venue, error=result.error)
        return

    leg.order_id = result.order_id

    # Market orders on these venues normally come back already filled. Poll
    # only when the venue says the order is still working.
    if str(result.status).lower() in {"filled"} and result.filled_shares > 0:
        orders.apply_fill(
            leg,
            {
                "id": result.order_id,
                "status": result.status,
                "filled_shares": result.filled_shares,
                "price": result.avg_price,
                "fee": result.fee,
            },
        )
    else:
        final = await orders.poll_fill(
            adapter,
            result.order_id,
            timeout=settings.execution.fill_timeout_seconds,
            interval=settings.execution.fill_poll_interval_seconds,
        )
        orders.apply_fill(leg, final)

    trade.record(
        "leg_result",
        venue=leg.venue,
        order_id=leg.order_id,
        status=leg.status.value,
        filled=leg.filled_shares,
        price=leg.avg_fill_price,
    )


async def _chase_shortfall(
    adapter: VenueAdapter,
    leg: TradeLeg,
    shortfall: float,
    settings: Settings,
    trade: Trade,
) -> float:
    """Try again for the missing shares at a worse price.

    Bounded by both the retry limit and the reprice ceiling: we will pay
    more to complete a hedge, but only up to the point where paying more
    costs more than the position is worth. Past that, unwinding is cheaper.
    """
    filled_extra = 0.0
    remaining = shortfall
    ceiling = settings.execution.second_leg_reprice_ceiling
    base_price = leg.intended_price

    for attempt in range(1, settings.execution.second_leg_retry_limit + 1):
        if remaining <= 1e-9:
            break

        # Step the price towards the ceiling on each attempt.
        step = ceiling * (attempt / max(settings.execution.second_leg_retry_limit, 1))
        price = round(base_price + step, 4)
        if step > ceiling + 1e-9 or price >= 1.0:
            trade.record("chase_abandoned", venue=leg.venue, reason="reprice ceiling")
            break

        venue_settings = settings.venues.get(leg.venue)
        order_type = venue_settings.order_type if venue_settings else "market"
        log(
            logger,
            logging.WARNING,
            "chasing unfilled leg at a worse price",
            venue=leg.venue,
            attempt=attempt,
            shortfall=round(remaining, 6),
            price=price,
            base_price=base_price,
        )
        trade.record(
            "chase_attempt",
            venue=leg.venue,
            attempt=attempt,
            shares=round(remaining, 6),
            price=price,
        )

        result = await orders.submit(
            adapter,
            leg,
            remaining,
            order_type=order_type,
            price=price if order_type == "limit" else None,
            slippage_pct=(
                (venue_settings.slippage_pct if venue_settings else 2.0) + attempt * 3.0
            ),
        )
        if not result.ok:
            trade.record("chase_failed", venue=leg.venue, error=result.error)
            continue

        filled = result.filled_shares
        if filled <= 0 and result.order_id:
            final = await orders.poll_fill(
                adapter,
                result.order_id,
                timeout=settings.execution.fill_timeout_seconds,
                interval=settings.execution.fill_poll_interval_seconds,
            )
            filled = float(final.get("filled_shares") or 0.0)
            price_filled = float(final.get("price") or price)
        else:
            price_filled = result.avg_price or price

        if filled > 0:
            # Blend the new fill into the leg's average.
            total_shares = leg.filled_shares + filled
            leg.avg_fill_price = round(
                (leg.filled_shares * leg.avg_fill_price + filled * price_filled)
                / total_shares,
                6,
            )
            leg.filled_shares = round(total_shares, 6)
            leg.cost = round(leg.filled_shares * leg.avg_fill_price, 6)
            leg.status = (
                LegStatus.FILLED
                if leg.filled_shares >= leg.intended_shares - 1e-9
                else LegStatus.PARTIAL
            )
            filled_extra += filled
            remaining -= filled
            trade.record(
                "chase_filled",
                venue=leg.venue,
                attempt=attempt,
                filled=filled,
                price=price_filled,
            )

    return filled_extra


async def execute_pair(
    trade: Trade,
    adapters: dict[str, VenueAdapter],
    settings: Settings,
    shares: float,
) -> ExecutionOutcome:
    """Place both legs and guarantee that what remains is hedged or nothing."""
    leg_a, leg_b = trade.legs[0], trade.legs[1]
    adapter_a, adapter_b = adapters[leg_a.venue], adapters[leg_b.venue]

    trade.status = TradeStatus.PENDING
    trade.record("execution_started", shares=shares, venues=[leg_a.venue, leg_b.venue])

    # 1. Both legs at once. The gap between two sequential submissions is
    # the window in which the second price moves away from you.
    await asyncio.gather(
        _submit_and_track(adapter_a, leg_a, shares, settings, trade),
        _submit_and_track(adapter_b, leg_b, shares, settings, trade),
        return_exceptions=False,
    )

    hedged = min(leg_a.filled_shares, leg_b.filled_shares)
    trade.record(
        "fills_compared",
        a_filled=leg_a.filled_shares,
        b_filled=leg_b.filled_shares,
        hedged=hedged,
    )

    # 2. Nothing filled anywhere — clean failure, nothing to contain.
    if leg_a.filled_shares <= 0 and leg_b.filled_shares <= 0:
        trade.status = TradeStatus.FAILED
        trade.record("execution_failed", reason="neither leg filled")
        for leg, adapter in ((leg_a, adapter_a), (leg_b, adapter_b)):
            if leg.order_id:
                await orders.cancel_quietly(adapter, leg.order_id)
        return ExecutionOutcome(trade=trade, notes=["neither leg filled"])

    # 3. Imbalance. Whichever side filled less leaves the other side naked
    # for the difference.
    short_leg, short_adapter, long_leg = (
        (leg_a, adapter_a, leg_b)
        if leg_a.filled_shares < leg_b.filled_shares
        else (leg_b, adapter_b, leg_a)
    )
    shortfall = round(long_leg.filled_shares - short_leg.filled_shares, 6)

    if shortfall <= 1e-9:
        # Both legs filled the same size. That is a hedge.
        trade.status = TradeStatus.OPEN
        trade.actual_cost = round(leg_a.cost + leg_a.fee + leg_b.cost + leg_b.fee, 6)
        trade.record("hedged", shares=hedged, cost=trade.actual_cost)
        log(
            logger,
            logging.INFO,
            "both legs filled — position hedged",
            trade_id=trade.id,
            shares=hedged,
            cost=trade.actual_cost,
        )
        return ExecutionOutcome(trade=trade, hedged_shares=hedged)

    # 4. We are exposed. Chase the short leg before giving up on it.
    log(
        logger,
        logging.WARNING,
        "UNMATCHED LEG — attempting containment",
        trade_id=trade.id,
        short_venue=short_leg.venue,
        shortfall=shortfall,
        long_venue=long_leg.venue,
    )
    trade.unmatched_leg = True
    trade.record(
        "unmatched_leg",
        short_venue=short_leg.venue,
        shortfall=shortfall,
        long_venue=long_leg.venue,
    )

    if settings.execution.second_leg_retry_limit > 0:
        await _chase_shortfall(short_adapter, short_leg, shortfall, settings, trade)

    hedged = min(leg_a.filled_shares, leg_b.filled_shares)
    excess = round(long_leg.filled_shares - hedged, 6)

    if excess <= 1e-9:
        # The chase completed the hedge. Still recorded as an unmatched-leg
        # event: the execution layer had to work to contain it, and that is
        # the number that says whether this works.
        trade.status = TradeStatus.OPEN
        trade.actual_cost = round(leg_a.cost + leg_a.fee + leg_b.cost + leg_b.fee, 6)
        trade.record("hedged_after_chase", shares=hedged)
        return ExecutionOutcome(
            trade=trade,
            hedged_shares=hedged,
            unmatched=True,
            notes=["second leg completed on retry"],
        )

    # 5. Still exposed. Unwind the excess.
    long_adapter = adapters[long_leg.venue]
    trade.unwind_attempted = True
    result = await unwind.unwind_leg(
        long_adapter, long_leg, settings, trade=trade, shares=excess
    )
    trade.unwind_succeeded = result.succeeded
    trade.containment_cost = result.cost

    if result.succeeded:
        trade.status = TradeStatus.CONTAINED if hedged <= 0 else TradeStatus.OPEN
        trade.actual_cost = round(leg_a.cost + leg_a.fee + leg_b.cost + leg_b.fee, 6)
        if hedged <= 0:
            trade.realised_pnl = -abs(result.cost)
            trade.settlement_notes = (
                "single-leg fill; filled leg unwound at market. "
                f"Containment cost {result.cost:.4f}."
            )
        return ExecutionOutcome(
            trade=trade,
            hedged_shares=hedged,
            unmatched=True,
            containment_cost=result.cost,
            notes=[f"unwound {result.shares_unwound} shares on {long_leg.venue}"],
        )

    # 6. Unwind failed. This is the worst state and it is reported as such.
    trade.status = TradeStatus.EXPOSED
    trade.settlement_notes = (
        f"UNWIND FAILED on {long_leg.venue}: {result.error}. "
        f"{excess} shares of '{long_leg.market_title}' are unhedged."
    )
    log(
        logger,
        logging.ERROR,
        "TRADE EXPOSED — unwind failed, manual intervention required",
        trade_id=trade.id,
        venue=long_leg.venue,
        shares=excess,
    )
    return ExecutionOutcome(
        trade=trade,
        hedged_shares=hedged,
        unmatched=True,
        containment_cost=result.cost,
        exposed=True,
        notes=[f"unwind failed on {long_leg.venue}: {result.error}"],
    )
