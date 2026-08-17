"""Fee and margin tests.

The failure mode being guarded against: a margin that looks positive because
fees were ignored or because the price used was the top of book rather than
the price the full size would actually pay.
"""

from __future__ import annotations

import math

import pytest

from margin import calculator
from margin.fees import fee_for, flat_bps_fee, kalshi_fee
from models import BookLevel, OrderBook
from settings.schema import FeeModel
from tests.conftest import make_book, make_market


# --- fees -----------------------------------------------------------------


def test_kalshi_fee_matches_the_published_formula():
    """fee = ceil(0.07 x C x P x (1-P)), rounded up to the cent."""
    # 100 contracts at 50c: 0.07 * 100 * 0.5 * 0.5 = 1.75
    assert kalshi_fee(100, 0.50) == pytest.approx(1.75)
    # 100 at 20c: 0.07 * 100 * 0.2 * 0.8 = 1.12
    assert kalshi_fee(100, 0.20) == pytest.approx(1.12)
    # Rounding is always up, never to nearest.
    raw = 0.07 * 33 * 0.37 * 0.63
    assert kalshi_fee(33, 0.37) == pytest.approx(math.ceil(raw * 100) / 100)


def test_kalshi_fee_peaks_at_the_middle_of_the_book():
    """The quadratic bites hardest where the arbitrage usually is.

    Treating this as a flat percentage would understate cost at exactly the
    prices that matter.
    """
    at_mid = kalshi_fee(1000, 0.50)
    at_tail = kalshi_fee(1000, 0.05)
    assert at_mid > at_tail * 5
    # And it is symmetric about 0.5.
    assert kalshi_fee(1000, 0.30) == pytest.approx(kalshi_fee(1000, 0.70))


def test_kalshi_fee_is_zero_outside_the_tradeable_range():
    assert kalshi_fee(100, 0.0) == 0.0
    assert kalshi_fee(100, 1.0) == 0.0
    assert kalshi_fee(0, 0.5) == 0.0


def test_flat_bps_is_a_share_of_notional():
    # 100 shares at 40c = $40 notional; 50bps = $0.20
    assert flat_bps_fee(100, 0.40, 50) == pytest.approx(0.20)


def test_fee_model_dispatch_and_fixed_costs():
    assert fee_for(FeeModel(model="none"), 100, 0.5) == 0.0
    assert fee_for(
        FeeModel(model="kalshi_quadratic", quadratic_rate=0.07), 100, 0.5
    ) == pytest.approx(1.75)
    # A fixed per-order cost makes small trades correctly uneconomic.
    model = FeeModel(model="none", fixed_cost_per_order=0.25)
    assert fee_for(model, 10, 0.5) == pytest.approx(0.25)


# --- depth ----------------------------------------------------------------


def test_walk_book_prices_size_against_the_ladder():
    """VWAP, not top of book."""
    book = OrderBook(
        outcome_id="o1",
        asks=[
            BookLevel(price=0.40, size=100),
            BookLevel(price=0.45, size=100),
            BookLevel(price=0.50, size=100),
        ],
        depth_source="book",
    )
    # 150 shares: 100 at 0.40 + 50 at 0.45 = 62.5, VWAP 0.41666
    fill = calculator.walk_book(book, 150)
    assert fill.shares == pytest.approx(150)
    assert fill.cost == pytest.approx(62.5)
    assert fill.avg_price == pytest.approx(62.5 / 150)
    assert fill.avg_price > book.asks[0].price  # worse than the top
    assert fill.complete is True


def test_walk_book_reports_what_it_could_not_fill():
    book = OrderBook(
        outcome_id="o1", asks=[BookLevel(price=0.40, size=50)], depth_source="book"
    )
    fill = calculator.walk_book(book, 200)
    assert fill.shares == pytest.approx(50)
    assert fill.complete is False
    assert fill.depth_available == pytest.approx(50)


def test_walk_book_handles_an_empty_book():
    fill = calculator.walk_book(OrderBook(outcome_id="o1"), 100)
    assert fill.shares == 0
    assert fill.complete is False


# --- margin ---------------------------------------------------------------


def test_margin_is_net_of_both_venues_fees():
    """The same prices give a different answer once Kalshi's fee is applied."""
    market_a = make_market(venue="polymarket", market_id="pa")
    market_b = make_market(venue="kalshi", market_id="kb")
    book_a = make_book("pa-yes", ask_price=0.45, ask_size=1000)
    book_b = make_book("kb-no", ask_price=0.52, ask_size=1000)

    free = calculator.evaluate(
        market_a, market_a.outcomes[0], book_a, FeeModel(model="none"),
        market_b, market_b.outcomes[1], book_b, FeeModel(model="none"),
        shares=100,
    )
    charged = calculator.evaluate(
        market_a, market_a.outcomes[0], book_a, FeeModel(model="none"),
        market_b, market_b.outcomes[1], book_b,
        FeeModel(model="kalshi_quadratic", quadratic_rate=0.07),
        shares=100,
    )

    # 0.45 + 0.52 = 0.97 gross, so ~3% before fees.
    assert free.net_margin > 0.03
    assert charged.net_margin < free.net_margin
    assert charged.total_fees == pytest.approx(kalshi_fee(100, 0.52))
    # The fee eats most of a 3-cent spread.
    assert charged.net_margin < 0.02


def test_margin_uses_the_size_both_books_can_support():
    """Legs are always equal size — an imbalance is a naked position."""
    market_a = make_market(venue="polymarket", market_id="pa")
    market_b = make_market(venue="kalshi", market_id="kb")
    deep = make_book("pa-yes", ask_price=0.45, ask_size=1000, levels=3)
    thin = OrderBook(
        outcome_id="kb-no",
        asks=[BookLevel(price=0.50, size=30)],
        depth_source="book",
    )

    result = calculator.evaluate(
        market_a, market_a.outcomes[0], deep, FeeModel(model="none"),
        market_b, market_b.outcomes[1], thin, FeeModel(model="none"),
        shares=500,
    )
    assert result.shares == pytest.approx(30)
    assert result.leg_a.shares == pytest.approx(result.leg_b.shares)
    assert result.complete is False


def test_payout_equals_one_dollar_per_share():
    """The invariant that makes this an arbitrage at all."""
    market_a = make_market(venue="polymarket", market_id="pa")
    market_b = make_market(venue="kalshi", market_id="kb")
    result = calculator.evaluate(
        market_a, market_a.outcomes[0], make_book("pa-yes", 0.45), FeeModel(model="none"),
        market_b, market_b.outcomes[1], make_book("kb-no", 0.50), FeeModel(model="none"),
        shares=100,
    )
    assert result.payout == pytest.approx(result.shares)
    assert result.profit == pytest.approx(result.payout - result.total_cost)


def test_a_losing_spread_reports_a_negative_margin():
    """No wishful arithmetic: 0.55 + 0.52 costs more than it can pay."""
    market_a = make_market(venue="polymarket", market_id="pa")
    market_b = make_market(venue="kalshi", market_id="kb")
    result = calculator.evaluate(
        market_a, market_a.outcomes[0], make_book("pa-yes", 0.55), FeeModel(model="none"),
        market_b, market_b.outcomes[1], make_book("kb-no", 0.52), FeeModel(model="none"),
        shares=100,
    )
    assert result.net_margin < 0
    assert result.profit < 0


def test_best_execution_finds_a_size_that_clears_the_floor():
    """A thin book should yield a smaller trade, not no trade."""
    market_a = make_market(venue="polymarket", market_id="pa")
    market_b = make_market(venue="kalshi", market_id="kb")
    # Cheap at the top, expensive above it.
    book_a = OrderBook(
        outcome_id="pa-yes",
        asks=[BookLevel(price=0.40, size=20), BookLevel(price=0.55, size=1000)],
        depth_source="book",
    )
    book_b = make_book("kb-no", ask_price=0.50, ask_size=1000)

    result = calculator.best_execution(
        market_a, market_a.outcomes[0], book_a, FeeModel(model="none"),
        market_b, market_b.outcomes[1], book_b, FeeModel(model="none"),
        max_stake=1000.0,
        min_margin=0.05,
    )
    assert result is not None
    assert result.net_margin >= 0.05
    # Sized by the book, not by the stake. At top-of-book prices the stake
    # would buy over a thousand shares; the expensive second level caps it
    # far below that.
    assert result.shares < 100
    assert result.profit > 0


def test_headline_margin_screens_before_a_book_is_fetched():
    good = calculator.headline_margin_from_prices(
        0.45, FeeModel(model="none"), 0.50, FeeModel(model="none")
    )
    bad = calculator.headline_margin_from_prices(
        0.60, FeeModel(model="none"), 0.55, FeeModel(model="none")
    )
    assert good > 0
    assert bad < 0
    # Missing prices are never treated as an opportunity.
    assert calculator.headline_margin_from_prices(
        None, FeeModel(model="none"), 0.5, FeeModel(model="none")
    ) < 0
