"""Matching and resolution tests.

The two failure modes that matter, in order of cost:

  1. Pairing two markets that are not the same question. Every downstream
     number is then meaningless and the "hedge" is two directional bets.
  2. Calling a pair MATCHED when the venues settle it differently. Same
     consequence, arrived at more convincingly.

So both the pairing tests and the resolution tests are written to catch
false positives rather than false negatives. A missed opportunity costs
nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from matching import pairing, resolution
from matching import text as textlib
from models import ResolutionStatus
from tests.conftest import make_market


# --- text -----------------------------------------------------------------


def test_number_extraction_normalises_venue_conventions():
    """$100k, 100,000 and 100000 are the same threshold."""
    assert 100_000 in textlib.extract_numbers("Will BTC hit $100k?")
    assert 100_000 in textlib.extract_numbers("Will BTC hit $100,000?")
    assert 100_000 in textlib.extract_numbers("BTC above 100000")
    assert 4_500_000_000 in textlib.extract_numbers("Revenue above $4.5b")


def test_date_extraction_normalises_formats():
    assert "2026-12-31" in textlib.extract_dates("by December 31, 2026")
    assert "2026-12-31" in textlib.extract_dates("by Dec 31 2026")
    assert "2026-12-31" in textlib.extract_dates("on 31st December 2026")
    assert "2026-12-31" in textlib.extract_dates("2026-12-31")
    # A missing year is filled from the market's own resolution date.
    assert "2026-12-31" in textlib.extract_dates("by Dec 31", default_year=2026)


def test_similarity_rates_paraphrases_above_unrelated_titles():
    same = textlib.similarity(
        "Will Bitcoin close above $100,000 on December 31?",
        "Bitcoin above $100,000 on Dec 31",
    )
    different = textlib.similarity(
        "Will Bitcoin close above $100,000 on December 31?",
        "Will the Lakers win the NBA championship?",
    )
    assert same > 0.5
    assert different < 0.2
    assert same > different


def test_synonyms_bridge_venue_vocabulary():
    assert textlib.similarity("Will BTC hit 100k", "Will Bitcoin hit 100k") > 0.8


# --- pairing --------------------------------------------------------------


def test_equivalent_markets_are_paired():
    a = [make_market(venue="polymarket", market_id="p1",
                     title="Will Bitcoin close above $100,000 on December 31, 2026?")]
    b = [make_market(venue="kalshi", market_id="k1",
                     title="Bitcoin above $100,000 on Dec 31, 2026?")]

    pairs = pairing.find_pairs(a, b, threshold=0.5)
    assert len(pairs) == 1
    assert pairs[0].market_a.market_id == "p1"
    assert pairs[0].market_b.market_id == "k1"


def test_different_thresholds_are_never_paired():
    """The dangerous false positive: near-identical prose, different number.

    These two score very high on raw string similarity and are completely
    different bets.
    """
    a = [make_market(venue="polymarket", market_id="p1",
                     title="Will Bitcoin close above $100,000 on December 31, 2026?")]
    b = [make_market(venue="kalshi", market_id="k1",
                     title="Will Bitcoin close above $120,000 on December 31, 2026?")]

    # Confirm the trap is real before confirming we avoid it.
    assert textlib.similarity(a[0].title, b[0].title) > 0.8
    assert pairing.find_pairs(a, b, threshold=0.5) == []


def test_different_dates_are_never_paired():
    a = [make_market(venue="polymarket", market_id="p1",
                     title="Will Bitcoin close above $100,000 on December 31, 2026?")]
    b = [make_market(venue="kalshi", market_id="k1",
                     title="Will Bitcoin close above $100,000 on November 30, 2026?")]
    assert pairing.find_pairs(a, b, threshold=0.5) == []


def test_competing_candidates_are_never_paired():
    """The false positive that live data surfaced.

    "Will Italy win the 2030 World Cup?" and "Will England win the 2030 World
    Cup?" score 0.81 on prose and share the token "FIFA". Buying Italy YES
    against England NO is not a hedge — it is two positions that can both
    lose.
    """
    a = [make_market(venue="polymarket", market_id="p1",
                     title="Will Italy win the 2030 FIFA Men's World Cup?")]
    b = [make_market(venue="kalshi", market_id="k1",
                     title="Will England win the 2030 FIFA Men's World Cup?")]

    assert textlib.similarity(a[0].title, b[0].title) > 0.8  # the trap is real
    assert pairing.find_pairs(a, b, threshold=0.5) == []


def test_different_subjects_are_never_paired():
    """Shared prose, different subject. Same class of error, different shape."""
    a = [make_market(venue="polymarket", market_id="p1",
                     title="Will Ebba Busch be the next Prime Minister of Sweden?")]
    b = [make_market(venue="kalshi", market_id="k1",
                     title="Will Naftali Bennett be the next Prime Minister of Israel?")]
    assert pairing.find_pairs(a, b, threshold=0.3) == []


def test_one_sided_extra_context_still_pairs():
    """A venue naming its price source must not break the match.

    Rejecting on any entity difference would be too strict: venues routinely
    add context on one side only.
    """
    a = [make_market(venue="polymarket", market_id="p1",
                     title="Will Bitcoin close above $100,000 on December 31, 2026?")]
    b = [make_market(venue="kalshi", market_id="k1",
                     title="Will BTC close above $100,000 on Binance by Dec 31, 2026?")]
    assert len(pairing.find_pairs(a, b, threshold=0.5)) == 1


def test_polymarket_compound_titles_match_bare_questions():
    """Polymarket prefixes its event name; Kalshi does not.

    Scored on the full titles alone, a genuine match lands around 0.5 and is
    discarded. The question part scores 1.0.
    """
    compound = ("Democratic Presidential Nominee 2028 - "
                "Will Wes Moore win the 2028 Democratic presidential nomination?")
    bare = "Will Wes Moore win the 2028 Democratic presidential nomination?"

    assert textlib.question_part(compound) == bare
    assert textlib.similarity(compound, bare) == pytest.approx(1.0)

    # A hyphen inside a single short question is left alone.
    assert textlib.question_part("Fed decision - September") == "Fed decision - September"


def test_pairing_is_one_to_one():
    """One market cannot be hedged against two counterparties at once."""
    a = [
        make_market(venue="polymarket", market_id="p1", title="Will the Lakers win the 2027 NBA title?"),
        make_market(venue="polymarket", market_id="p2", title="Will the Lakers win the 2027 NBA title?"),
    ]
    b = [make_market(venue="kalshi", market_id="k1", title="Lakers to win the 2027 NBA title")]

    pairs = pairing.find_pairs(a, b, threshold=0.5)
    assert len(pairs) == 1
    assert len({p.market_b.market_id for p in pairs}) == 1


def test_unrelated_markets_are_not_paired():
    a = [make_market(venue="polymarket", market_id="p1", title="Will the Lakers win the 2027 NBA title?")]
    b = [make_market(venue="kalshi", market_id="k1", title="Will inflation exceed 4% in June 2026?")]
    assert pairing.find_pairs(a, b, threshold=0.72) == []


def test_outcome_alignment_finds_both_hedge_directions():
    """The hedge is YES on one venue against NO on the other, both ways."""
    pair = pairing.PairCandidate(
        market_a=make_market(venue="polymarket", market_id="p1"),
        market_b=make_market(venue="kalshi", market_id="k1",
                             yes_label="Mars", no_label="Not Mars"),
        score=0.9,
    )
    pairing.align_outcomes(pair)

    assert len(pair.complements) == 2
    a_first, b_first = pair.complements[0]
    a_second, b_second = pair.complements[1]
    # Each direction buys opposite sides on the two venues.
    assert a_first.outcome_id.endswith("-yes")
    assert b_first.outcome_id.endswith("-no")
    assert a_second.outcome_id.endswith("-no")
    assert b_second.outcome_id.endswith("-yes")


def test_negation_labels_are_recognised():
    """Venues label the NO side by negating the YES label, not "No"."""
    market = make_market(venue="kalshi", market_id="k1",
                         yes_label="Mars", no_label="Not Mars")
    yes, no = pairing._identify_sides(market)
    assert yes.label == "Mars"
    assert no.label == "Not Mars"

    # And when the venue lists the negation first.
    reversed_market = make_market(venue="kalshi", market_id="k2",
                                  yes_label="Not Mars", no_label="Mars")
    yes, no = pairing._identify_sides(reversed_market)
    assert yes.label == "Mars"
    assert no.label == "Not Mars"


# --- resolution -----------------------------------------------------------

FUTURE = datetime.now(timezone.utc) + timedelta(days=60)

BINANCE_TOUCH = (
    "This market will resolve to Yes if any Binance 1 minute candle for "
    "BTC/USDT has a final Low price equal to or lower than $60,000 before "
    "11:59 PM ET on December 31, 2026."
)
BINANCE_CLOSE = (
    "This market resolves Yes if the Binance BTC/USDT closing price at "
    "11:59 PM ET on December 31, 2026 is above $60,000."
)
COINBASE_CLOSE = (
    "This market resolves Yes if the Coinbase BTC-USD closing price at "
    "11:59 PM ET on December 31, 2026 is above $60,000."
)


def test_identical_criteria_are_matched():
    a = make_market(venue="polymarket", market_id="p1",
                    description=BINANCE_CLOSE, resolution_date=FUTURE)
    b = make_market(venue="kalshi", market_id="k1",
                    description=BINANCE_CLOSE, resolution_date=FUTURE)

    assessment = resolution.compare(a, b)
    assert assessment.status is ResolutionStatus.MATCHED
    assert assessment.checks["basis"] == "match"
    assert assessment.checks["source"] == "match"
    assert assessment.checks["settlement_time"] == "match"


def test_intraday_touch_versus_close_differs():
    """The trap this whole module exists for.

    Same asset, same threshold, same deadline — and a completely different
    question. One asks whether the price ever touched the level, the other
    whether it finished above it.
    """
    a = make_market(venue="polymarket", market_id="p1",
                    description=BINANCE_TOUCH, resolution_date=FUTURE)
    b = make_market(venue="kalshi", market_id="k1",
                    description=BINANCE_CLOSE, resolution_date=FUTURE)

    assessment = resolution.compare(a, b)
    assert assessment.status is ResolutionStatus.DIFFERS
    assert assessment.checks["basis"] == "differ"
    assert any("basis differs" in note for note in assessment.notes)


def test_different_price_sources_differ():
    a = make_market(venue="polymarket", market_id="p1",
                    description=BINANCE_CLOSE, resolution_date=FUTURE)
    b = make_market(venue="kalshi", market_id="k1",
                    description=COINBASE_CLOSE, resolution_date=FUTURE)

    assessment = resolution.compare(a, b)
    assert assessment.status is ResolutionStatus.DIFFERS
    assert assessment.checks["source"] == "differ"


def test_different_settlement_times_differ():
    a = make_market(venue="polymarket", market_id="p1",
                    description=BINANCE_CLOSE, resolution_date=FUTURE)
    b = make_market(venue="kalshi", market_id="k1",
                    description=BINANCE_CLOSE,
                    resolution_date=FUTURE + timedelta(days=7))

    assessment = resolution.compare(a, b)
    assert assessment.status is ResolutionStatus.DIFFERS
    assert assessment.checks["settlement_time"] == "differ"


def test_silence_is_unverified_not_matched():
    """Absence of evidence is not agreement. This is the default."""
    a = make_market(venue="polymarket", market_id="p1",
                    description="Will it happen?", resolution_date=FUTURE)
    b = make_market(venue="kalshi", market_id="k1",
                    description="Resolves Yes if it happens.", resolution_date=FUTURE)

    assessment = resolution.compare(a, b)
    assert assessment.status is ResolutionStatus.UNVERIFIED
    assert assessment.checks["basis"] == "unknown"


def test_empty_criteria_are_unverified():
    a = make_market(venue="polymarket", market_id="p1", description="", resolution_date=FUTURE)
    b = make_market(venue="kalshi", market_id="k1", description="", resolution_date=FUTURE)
    assert resolution.compare(a, b).status is ResolutionStatus.UNVERIFIED


def test_missing_settlement_dates_are_unverified():
    """A venue that publishes no resolution date cannot be confirmed."""
    a = make_market(venue="polymarket", market_id="p1", description=BINANCE_CLOSE)
    a.resolution_date = None
    b = make_market(venue="kalshi", market_id="k1",
                    description=BINANCE_CLOSE, resolution_date=FUTURE)

    assessment = resolution.compare(a, b)
    assert assessment.status is ResolutionStatus.UNVERIFIED
    assert assessment.checks["settlement_time"] == "unknown"


def test_assessment_carries_both_raw_texts():
    """The dashboard shows the criteria side by side; nothing is paraphrased."""
    a = make_market(venue="polymarket", market_id="p1",
                    description=BINANCE_TOUCH, resolution_date=FUTURE)
    b = make_market(venue="kalshi", market_id="k1",
                    description=BINANCE_CLOSE, resolution_date=FUTURE)

    assessment = resolution.compare(a, b)
    payload = assessment.to_dict()
    assert payload["resolution_a"] == BINANCE_TOUCH
    assert payload["resolution_b"] == BINANCE_CLOSE
    assert payload["resolution_notes"]
    assert payload["parsed_a"]["source"] == "binance"
    assert payload["parsed_a"]["basis"] == "intraday_touch"
    assert payload["parsed_b"]["basis"] == "close"


def test_cutoff_time_parsing():
    assert resolution.extract_cutoff("by 11:59 PM ET on Dec 31") == "23:59 ET"
    assert resolution.extract_cutoff("at 4 PM ET") == "16:00 ET"
    assert resolution.extract_cutoff("at 12:00 UTC") == "12:00 UTC"
    assert resolution.extract_cutoff("no time here") is None
