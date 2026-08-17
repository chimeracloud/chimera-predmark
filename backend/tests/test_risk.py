"""Pre-trade check tests.

Section 2.2 of the brief lists checks that must all pass. Each one is tested
by making it the *only* thing wrong, so a check that silently stopped working
fails a test rather than quietly permitting a trade.
"""

from __future__ import annotations

import pytest

from models import LegQuote, Opportunity, ResolutionAssessment, ResolutionStatus
from risk import limits
from tests.conftest import make_settings


def _opportunity(
    margin: float = 0.05,
    status: ResolutionStatus = ResolutionStatus.MATCHED,
    shares: float = 100.0,
    depth: float = 5000.0,
    price_a: float = 0.45,
    price_b: float = 0.50,
) -> Opportunity:
    return Opportunity(
        leg_a=LegQuote(
            venue="polymarket", market_id="p1", outcome_id="p1-yes",
            outcome_label="Yes", market_title="Will X?",
            avg_price=price_a, best_price=price_a, shares=shares,
            cost=price_a * shares, depth_available=depth, depth_source="book",
        ),
        leg_b=LegQuote(
            venue="kalshi", market_id="k1", outcome_id="k1-no",
            outcome_label="No", market_title="Will X?",
            avg_price=price_b, best_price=price_b, shares=shares,
            cost=price_b * shares, depth_available=depth, depth_source="book",
        ),
        shares=shares,
        total_cost=(price_a + price_b) * shares,
        net_margin=margin,
        resolution=ResolutionAssessment(status=status),
    )


def _context(**overrides) -> limits.RiskContext:
    context = limits.RiskContext(
        settings=overrides.pop("settings", make_settings()),
        balances={
            "polymarket": {"available": 10_000.0, "total": 10_000.0, "locked": 0.0},
            "kalshi": {"available": 10_000.0, "total": 10_000.0, "locked": 0.0},
        },
        credentialed_venues={"polymarket", "kalshi"},
    )
    for key, value in overrides.items():
        setattr(context, key, value)
    return context


def test_a_good_opportunity_is_allowed():
    """The baseline. If this fails the other tests prove nothing."""
    decision = limits.evaluate(_opportunity(), _context())
    assert decision.allowed is True
    assert decision.reasons == []


def test_kill_switch_blocks_everything_immediately():
    settings = make_settings()
    settings.risk.kill_switch = True
    settings.risk.kill_switch_reason = "manual halt"

    decision = limits.evaluate(_opportunity(), _context(settings=settings))
    assert decision.allowed is False
    assert "kill switch engaged" in decision.reasons[0]
    # Short-circuits: no point enumerating anything else.
    assert len(decision.reasons) == 1


def test_kill_switch_from_live_context_blocks_even_if_settings_are_stale():
    """The fresh Firestore read wins over the settings snapshot.

    This is what makes the switch work mid-scan without a restart.
    """
    decision = limits.evaluate(_opportunity(), _context(killed=True))
    assert decision.allowed is False


def test_trading_disabled_blocks():
    settings = make_settings()
    settings.execution.trading_enabled = False
    decision = limits.evaluate(_opportunity(), _context(settings=settings))
    assert decision.allowed is False
    assert "trading is disabled" in decision.reasons[0]


def test_unverified_resolution_is_not_tradeable_by_default():
    """UNVERIFIED is recorded and displayed, never traded."""
    decision = limits.evaluate(
        _opportunity(status=ResolutionStatus.UNVERIFIED), _context()
    )
    assert decision.allowed is False
    assert any("UNVERIFIED" in reason for reason in decision.reasons)


def test_differing_resolution_is_never_tradeable():
    """Not even with the override — DIFFERS means two different bets."""
    settings = make_settings()
    settings.risk.allow_unverified_override = True
    settings.risk.required_resolution_status = "MATCHED_OR_UNVERIFIED"

    decision = limits.evaluate(
        _opportunity(status=ResolutionStatus.DIFFERS), _context(settings=settings)
    )
    assert decision.allowed is False
    assert any("resolution criteria differ" in r for r in decision.reasons)


def test_unverified_is_tradeable_only_under_an_explicit_override():
    settings = make_settings()
    settings.risk.allow_unverified_override = True
    settings.risk.required_resolution_status = "MATCHED_OR_UNVERIFIED"

    decision = limits.evaluate(
        _opportunity(status=ResolutionStatus.UNVERIFIED), _context(settings=settings)
    )
    assert decision.allowed is True


def test_override_requires_both_switches():
    """One switch alone is not an override — deliberately."""
    settings = make_settings()
    settings.risk.allow_unverified_override = True
    # required_resolution_status left at MATCHED
    decision = limits.evaluate(
        _opportunity(status=ResolutionStatus.UNVERIFIED), _context(settings=settings)
    )
    assert decision.allowed is False


def test_margin_below_the_floor_blocks():
    settings = make_settings()
    settings.margin.min_margin_to_trade = 0.03
    decision = limits.evaluate(_opportunity(margin=0.01), _context(settings=settings))
    assert decision.allowed is False
    assert any("below floor" in r for r in decision.reasons)


def test_implausible_margin_blocks():
    """A 1090% margin is a broken feed, not free money.

    Observed live: Limitless served the identical order book for both
    outcomes of a market, so YES and NO both quoted 0.044 and the pair
    presented an enormous arbitrage that does not exist. Everything else
    about it passed — the questions were identical and resolution was
    MATCHED.
    """
    decision = limits.evaluate(_opportunity(margin=10.9), _context())
    assert decision.allowed is False
    assert any("plausibility ceiling" in r for r in decision.reasons)


def test_a_normal_spread_is_not_caught_by_the_ceiling():
    decision = limits.evaluate(_opportunity(margin=0.04), _context())
    assert decision.allowed is True


def test_insufficient_depth_blocks():
    decision = limits.evaluate(_opportunity(shares=100, depth=50), _context())
    assert decision.allowed is False
    assert any("book holds" in r for r in decision.reasons)


def test_missing_depth_information_blocks():
    opportunity = _opportunity()
    opportunity.leg_a.depth_source = "none"
    decision = limits.evaluate(opportunity, _context())
    assert decision.allowed is False
    assert any("no depth information" in r for r in decision.reasons)


def test_insufficient_balance_blocks():
    context = _context()
    context.balances["kalshi"] = {"available": 1.0, "total": 1.0, "locked": 0.0}
    decision = limits.evaluate(_opportunity(), context)
    assert decision.allowed is False
    assert any("available balance" in r for r in decision.reasons)


def test_unknown_balance_blocks_rather_than_assuming_zero_or_infinite():
    context = _context()
    del context.balances["kalshi"]
    decision = limits.evaluate(_opportunity(), context)
    assert decision.allowed is False
    assert any("balance unknown" in r for r in decision.reasons)


def test_missing_credentials_block():
    context = _context(credentialed_venues={"polymarket"})
    decision = limits.evaluate(_opportunity(), context)
    assert decision.allowed is False
    assert any("no usable credentials" in r for r in decision.reasons)


def test_disabled_venue_blocks():
    settings = make_settings()
    settings.venues["kalshi"].enabled = False
    decision = limits.evaluate(_opportunity(), _context(settings=settings))
    assert decision.allowed is False
    assert any("not enabled" in r for r in decision.reasons)


def test_concurrency_cap_blocks():
    settings = make_settings()
    settings.execution.max_concurrent_trades = 1
    decision = limits.evaluate(_opportunity(), _context(settings=settings, open_trade_count=1))
    assert decision.allowed is False
    assert any("already open" in r for r in decision.reasons)


def test_daily_trade_limit_blocks():
    settings = make_settings()
    settings.risk.daily_trade_limit = 5
    decision = limits.evaluate(_opportunity(), _context(settings=settings, trades_today=5))
    assert decision.allowed is False
    assert any("daily trade limit" in r for r in decision.reasons)


def test_daily_loss_limit_blocks():
    settings = make_settings()
    settings.risk.daily_loss_limit = 50.0
    decision = limits.evaluate(
        _opportunity(), _context(settings=settings, realised_pnl_today=-50.0)
    )
    assert decision.allowed is False
    assert any("daily loss limit" in r for r in decision.reasons)


def test_per_venue_exposure_cap_blocks():
    settings = make_settings()
    settings.venues["polymarket"].max_exposure = 10.0
    decision = limits.evaluate(_opportunity(), _context(settings=settings))
    assert decision.allowed is False
    assert any("exposure would reach" in r for r in decision.reasons)


def test_exposure_is_summed_across_venues_not_netted():
    """Positions cannot be netted across platforms.

    A 45-dollar leg and a 50-dollar leg consume 95 dollars of the global cap,
    not 5.
    """
    settings = make_settings()
    settings.risk.max_total_exposure = 90.0  # below 45 + 50, above the difference
    decision = limits.evaluate(_opportunity(shares=100), _context(settings=settings))
    assert decision.allowed is False
    assert any("total exposure would reach" in r for r in decision.reasons)


def test_extreme_prices_block():
    """The tails are where stale quotes and phantom liquidity live."""
    decision = limits.evaluate(_opportunity(price_a=0.001, price_b=0.5), _context())
    assert decision.allowed is False
    assert any("outside" in r for r in decision.reasons)


def test_unmatched_leg_pile_up_halts_trading():
    """The execution layer failing must not be allowed to continue."""
    settings = make_settings()
    settings.alerts.halt_on_unmatched_legs = 3
    decision = limits.evaluate(
        _opportunity(), _context(settings=settings, unmatched_legs_today=3)
    )
    assert decision.allowed is False
    assert any("unmatched legs today" in r for r in decision.reasons)


def test_all_failures_are_reported_not_just_the_first():
    """Charles should see everything wrong at once, not fix them one by one."""
    settings = make_settings()
    settings.margin.min_margin_to_trade = 0.10
    decision = limits.evaluate(
        _opportunity(margin=0.001, status=ResolutionStatus.UNVERIFIED, depth=1),
        _context(settings=settings),
    )
    assert decision.allowed is False
    assert len(decision.reasons) >= 3
