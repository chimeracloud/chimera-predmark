"""Execution tests — the ones that decide whether this works.

Item 5 of the brief: "Execution places both legs, detects single-leg fills,
and unwinds. Test it hard."

The invariant under test throughout: after `execute_pair` returns, the
position is either fully hedged or fully closed. There is no path that leaves
a filled leg standing against an unfilled one without either containing it or
reporting EXPOSED.
"""

from __future__ import annotations

import pytest

from execution import legs as leg_engine
from models import LegQuote, LegStatus, Trade, TradeStatus
from tests.conftest import FakeAdapter, make_settings


def _quote(venue: str, price: float, outcome: str = "yes") -> LegQuote:
    return LegQuote(
        venue=venue,
        market_id=f"{venue}-m1",
        outcome_id=f"{venue}-{outcome}",
        outcome_label=outcome.upper(),
        market_title="Will X happen?",
        avg_price=price,
        best_price=price,
        shares=100.0,
        cost=price * 100.0,
        depth_available=5000.0,
        depth_source="book",
    )


def _trade(shares: float = 100.0) -> Trade:
    trade = Trade(opportunity_id="opp1", intended_shares=shares)
    trade.legs = [
        leg_engine.build_leg(_quote("polymarket", 0.45), shares),
        leg_engine.build_leg(_quote("kalshi", 0.52, "no"), shares),
    ]
    return trade


@pytest.mark.asyncio
async def test_both_legs_fill_leaves_hedged_position():
    """The happy path: equal fills on both venues, nothing to contain."""
    settings = make_settings()
    adapters = {
        "polymarket": FakeAdapter(venue="polymarket", fill_ratio=1.0, fill_price=0.45),
        "kalshi": FakeAdapter(venue="kalshi", fill_ratio=1.0, fill_price=0.52),
    }
    trade = _trade()

    outcome = await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    assert trade.status is TradeStatus.OPEN
    assert outcome.hedged_shares == 100.0
    assert outcome.unmatched is False
    assert outcome.exposed is False
    assert all(leg.status is LegStatus.FILLED for leg in trade.legs)
    # No sell orders: nothing needed unwinding.
    assert all(adapter.sell_calls == 0 for adapter in adapters.values())


@pytest.mark.asyncio
async def test_single_leg_fill_is_unwound():
    """The test item 5 hinges on.

    Leg A fills, leg B does not. The filled leg must be sold back out, the
    trade recorded as a containment failure with its realised cost, and no
    position left standing.
    """
    settings = make_settings()
    settings.execution.second_leg_retry_limit = 0  # straight to unwind
    adapters = {
        "polymarket": FakeAdapter(venue="polymarket", fill_ratio=1.0, fill_price=0.45),
        "kalshi": FakeAdapter(venue="kalshi", fill_ratio=0.0, fill_price=0.52),
    }
    trade = _trade()

    outcome = await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    # The unmatched leg was detected.
    assert outcome.unmatched is True
    assert trade.unmatched_leg is True

    # The filled leg was sold back out.
    assert adapters["polymarket"].sell_calls == 1
    assert trade.legs[0].status is LegStatus.UNWOUND
    assert trade.legs[0].unwind_shares == pytest.approx(100.0)

    # Nothing is left hedged or exposed, and the cost is recorded.
    assert outcome.hedged_shares == 0.0
    assert outcome.exposed is False
    assert trade.status is TradeStatus.CONTAINED
    assert trade.containment_cost is not None and trade.containment_cost > 0
    # Bought at 0.45, sold at 0.95 of that — the loss is real and booked.
    assert trade.realised_pnl == pytest.approx(-trade.containment_cost)

    # The audit trail records what happened, in order.
    events = [event["event"] for event in trade.events]
    assert "unmatched_leg" in events
    assert "unwind_started" in events
    assert "unwind_completed" in events


@pytest.mark.asyncio
async def test_single_leg_fill_is_chased_before_unwinding():
    """Chasing the second leg is tried before giving up on the hedge.

    Unwinding costs the spread twice. If the second leg can be completed at a
    slightly worse price, that is cheaper than closing out.
    """
    settings = make_settings()
    settings.execution.second_leg_retry_limit = 2
    settings.execution.second_leg_reprice_ceiling = 0.05

    kalshi = FakeAdapter(venue="kalshi", fill_ratio=0.0, fill_price=0.52)

    # The chase succeeds on the retry.
    original_create = kalshi.create_order

    async def fills_on_retry(*args, **kwargs):
        if kalshi.buy_calls >= 1:
            kalshi.fill_ratio = 1.0
        return await original_create(*args, **kwargs)

    kalshi.create_order = fills_on_retry  # type: ignore[method-assign]

    adapters = {
        "polymarket": FakeAdapter(venue="polymarket", fill_ratio=1.0, fill_price=0.45),
        "kalshi": kalshi,
    }
    trade = _trade()

    outcome = await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    assert outcome.hedged_shares == pytest.approx(100.0)
    assert trade.status is TradeStatus.OPEN
    # Recorded as unmatched even though it was contained — the execution
    # layer had to work, and that is the number worth watching.
    assert outcome.unmatched is True
    assert adapters["polymarket"].sell_calls == 0  # no unwind needed
    assert "chase_filled" in [event["event"] for event in trade.events]


@pytest.mark.asyncio
async def test_failed_unwind_reports_exposed():
    """When the unwind itself fails, say so. Loudly.

    This is the worst state the system can reach. It must never be reported
    as anything else, because it is the one that needs a human.
    """
    settings = make_settings()
    settings.execution.second_leg_retry_limit = 0
    adapters = {
        "polymarket": FakeAdapter(
            venue="polymarket", fill_ratio=1.0, fill_price=0.45, sell_fails=True
        ),
        "kalshi": FakeAdapter(venue="kalshi", fill_ratio=0.0),
    }
    trade = _trade()

    outcome = await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    assert outcome.exposed is True
    assert trade.status is TradeStatus.EXPOSED
    assert trade.unwind_succeeded is False
    assert trade.legs[0].status is LegStatus.UNWIND_FAILED
    assert "UNWIND FAILED" in trade.settlement_notes
    # Retried before giving up.
    assert adapters["polymarket"].sell_calls == settings.execution.unwind_retry_limit + 1


@pytest.mark.asyncio
async def test_partial_fills_unwind_only_the_excess():
    """Unequal partial fills leave the difference naked — only that is unwound.

    Leg A fills 100, leg B fills 60. Sixty shares are genuinely hedged and
    should be kept; the 40-share excess on A is exposure and must go.
    """
    settings = make_settings()
    settings.execution.second_leg_retry_limit = 0
    adapters = {
        "polymarket": FakeAdapter(venue="polymarket", fill_ratio=1.0, fill_price=0.45),
        "kalshi": FakeAdapter(venue="kalshi", fill_ratio=0.6, fill_price=0.52),
    }
    trade = _trade()

    outcome = await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    assert outcome.hedged_shares == pytest.approx(60.0)
    assert outcome.unmatched is True
    # Only the 40-share excess was sold.
    sell_orders = [o for o in adapters["polymarket"].orders if o.side == "sell"]
    assert len(sell_orders) == 1
    assert sell_orders[0].shares == pytest.approx(40.0)
    # The hedged remainder stays open.
    assert trade.status is TradeStatus.OPEN


@pytest.mark.asyncio
async def test_neither_leg_fills_is_a_clean_failure():
    """Nothing filled means nothing to contain — and no unwind attempts."""
    settings = make_settings()
    adapters = {
        "polymarket": FakeAdapter(venue="polymarket", fill_ratio=0.0),
        "kalshi": FakeAdapter(venue="kalshi", fill_ratio=0.0),
    }
    trade = _trade()

    outcome = await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    assert trade.status is TradeStatus.FAILED
    assert outcome.unmatched is False
    assert outcome.exposed is False
    assert all(adapter.sell_calls == 0 for adapter in adapters.values())


@pytest.mark.asyncio
async def test_ambiguous_submission_is_reconciled_against_the_venue():
    """A submission that errors in transit may still have reached the venue.

    Guessing means either abandoning a real position or unwinding one that
    does not exist. So we ask the venue what it actually holds.
    """
    from venues.pmxt_client import PmxtUnavailable

    settings = make_settings()
    settings.execution.second_leg_retry_limit = 0

    polymarket = FakeAdapter(
        venue="polymarket",
        raise_on_buy=PmxtUnavailable(
            "sidecar transport error", code="SIDECAR_UNAVAILABLE", retryable=True
        ),
    )
    # The venue does hold the position — the order landed despite the error.
    polymarket.positions_response = [
        {"outcome_id": "polymarket-yes", "size": 100.0, "entry_price": 0.45}
    ]
    adapters = {
        "polymarket": polymarket,
        "kalshi": FakeAdapter(venue="kalshi", fill_ratio=0.0),
    }
    trade = _trade()

    outcome = await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    # The hidden position was found and then contained.
    assert trade.legs[0].filled_shares == pytest.approx(100.0)
    assert outcome.unmatched is True
    assert polymarket.sell_calls == 1
    assert "leg_reconciled" in [event["event"] for event in trade.events]


@pytest.mark.asyncio
async def test_both_legs_are_submitted_concurrently():
    """Sequential submission is a window for the second price to move.

    Asserted by checking that neither leg's submission completed before the
    other started.
    """
    import asyncio

    settings = make_settings()
    order_of_events: list[str] = []

    def instrument(adapter: FakeAdapter, name: str):
        original = adapter.create_order

        async def wrapped(*args, **kwargs):
            order_of_events.append(f"{name}:start")
            await asyncio.sleep(0.05)
            result = await original(*args, **kwargs)
            order_of_events.append(f"{name}:end")
            return result

        adapter.create_order = wrapped  # type: ignore[method-assign]
        return adapter

    adapters = {
        "polymarket": instrument(
            FakeAdapter(venue="polymarket", fill_ratio=1.0, fill_price=0.45), "a"
        ),
        "kalshi": instrument(
            FakeAdapter(venue="kalshi", fill_ratio=1.0, fill_price=0.52), "b"
        ),
    }
    trade = _trade()

    await leg_engine.execute_pair(trade, adapters, settings, 100.0)

    # Both start before either finishes.
    assert order_of_events[:2] == ["a:start", "b:start"] or order_of_events[:2] == [
        "b:start",
        "a:start",
    ]
