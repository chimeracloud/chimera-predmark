"""Scan eligibility tests.

These encode data-quality invariants learned from live venue responses. Each
one exists because a real feed breached it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scan.runner import _eligible
from tests.conftest import make_market, make_settings

NOW = datetime.now(timezone.utc)
SOON = NOW + timedelta(days=30)


def _settings():
    settings = make_settings()
    settings.scanning.min_liquidity = 1000
    settings.scanning.min_volume_24h = 0
    settings.venues["polymarket"].min_liquidity = None
    settings.venues["polymarket"].min_volume_24h = None
    settings.venues["kalshi"].min_liquidity = None
    settings.venues["kalshi"].min_volume_24h = None
    return settings


def test_a_normal_market_is_eligible():
    market = make_market(yes_price=0.45, liquidity=50_000, resolution_date=SOON)
    assert _eligible(market, _settings(), NOW) is True


def test_outcomes_that_do_not_sum_to_one_are_rejected():
    """The Limitless failure, caught by arithmetic.

    Limitless returned the same order book for both outcomes of several
    markets, quoting YES at 0.044 and NO at 0.033. Holding both pays exactly
    $1, so they must price to about $1 between them. These did not, and the
    resulting pair showed a margin of over 1000%.
    """
    market = make_market(liquidity=50_000, resolution_date=SOON)
    market.outcomes[0].price = 0.044
    market.outcomes[1].price = 0.033
    assert _eligible(market, _settings(), NOW) is False


def test_a_small_complement_deviation_is_tolerated():
    """Bid-ask spread and rounding move the sum a little; that is normal."""
    market = make_market(liquidity=50_000, resolution_date=SOON)
    market.outcomes[0].price = 0.47
    market.outcomes[1].price = 0.55  # sums to 1.02
    assert _eligible(market, _settings(), NOW) is True


def test_per_venue_liquidity_floors_override_the_global_one():
    """Kalshi reports no liquidity and trades at a fraction of Polymarket's
    volume; one global floor cannot serve both."""
    settings = _settings()
    settings.scanning.min_liquidity = 10_000

    kalshi = make_market(venue="kalshi", liquidity=0, resolution_date=SOON)
    kalshi.volume_24h = 500
    assert _eligible(kalshi, settings, NOW) is False

    settings.venues["kalshi"].min_liquidity = 0
    settings.venues["kalshi"].min_volume_24h = 100
    assert _eligible(kalshi, settings, NOW) is True


def test_liquidity_falls_back_to_volume_where_a_venue_reports_none():
    settings = _settings()
    settings.scanning.min_liquidity = 1000
    market = make_market(venue="kalshi", liquidity=0, resolution_date=SOON)
    market.volume_24h = 25_000
    assert _eligible(market, settings, NOW) is True


def test_imminent_and_distant_resolutions_are_rejected():
    settings = _settings()

    imminent = make_market(liquidity=50_000, resolution_date=NOW + timedelta(minutes=10))
    assert _eligible(imminent, settings, NOW) is False

    distant = make_market(liquidity=50_000, resolution_date=NOW + timedelta(days=4000))
    assert _eligible(distant, settings, NOW) is False


def test_long_dated_markets_are_within_the_default_window():
    """Kalshi's liquid book is largely 2028 elections and the 2030 World Cup.

    A one-year cap excluded roughly nine tenths of it.
    """
    settings = _settings()
    market = make_market(liquidity=50_000, resolution_date=NOW + timedelta(days=900))
    assert _eligible(market, settings, NOW) is True


def test_excluded_keywords_are_applied():
    settings = _settings()
    settings.scanning.exclude_keywords = ["bitcoin"]
    market = make_market(liquidity=50_000, resolution_date=SOON)
    assert _eligible(market, settings, NOW) is False
