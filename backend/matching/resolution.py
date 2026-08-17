"""Resolution equivalence.

Two venues can ask what reads as the same question and settle it differently.
"Will BTC be above $100k on Dec 31" resolves one way against a Binance
one-minute low at any point in the period, and another way against a
Coinbase close at 4pm ET. Both look like the same bet. Holding both sides is
double the risk, not a hedge — and the spread that made it look attractive is
usually the market correctly pricing the difference.

This module extracts what can be extracted and refuses to guess at the rest.
It compares five dimensions:

    settlement_time   when the question is decided
    basis             closing price vs intraday touch vs official result
    source            who publishes the deciding number
    threshold         the number and its comparator
    expiry            the venue's own resolution timestamp

A conflict on any dimension is DIFFERS. Agreement is MATCHED only when the
dimensions that matter were actually determined on both sides — silence is
not agreement. Everything else is UNVERIFIED, which is the default, and
UNVERIFIED is never traded unless Charles explicitly overrides in settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from matching.text import normalise, significant_numbers
from models import Market, ResolutionAssessment, ResolutionStatus

# --- vocabulary -----------------------------------------------------------

# Who publishes the deciding number. Order matters: more specific first.
SOURCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("binance", re.compile(r"\bbinance\b", re.I)),
    ("coinbase", re.compile(r"\bcoinbase\b", re.I)),
    ("kraken", re.compile(r"\bkraken\b", re.I)),
    ("chainlink", re.compile(r"\bchainlink\b", re.I)),
    ("coinmarketcap", re.compile(r"\bcoinmarketcap\b|\bcmc\b", re.I)),
    ("coingecko", re.compile(r"\bcoingecko\b", re.I)),
    ("pyth", re.compile(r"\bpyth\b", re.I)),
    ("cme", re.compile(r"\bcme\b|chicago mercantile", re.I)),
    ("bls", re.compile(r"\bbls\b|bureau of labor", re.I)),
    ("bea", re.compile(r"\bbea\b|bureau of economic analysis", re.I)),
    ("federal_reserve", re.compile(r"federal reserve|\bfomc\b|\bfed\b", re.I)),
    ("associated_press", re.compile(r"associated press|\bap\b(?!\w)", re.I)),
    ("espn", re.compile(r"\bespn\b", re.I)),
    ("nyt", re.compile(r"new york times|\bnyt\b", re.I)),
    ("reuters", re.compile(r"\breuters\b", re.I)),
    ("nasdaq", re.compile(r"\bnasdaq\b", re.I)),
    ("nyse", re.compile(r"\bnyse\b", re.I)),
    ("yahoo_finance", re.compile(r"yahoo finance", re.I)),
    ("official", re.compile(r"official (?:results?|announcement|source|website)", re.I)),
]

# How the outcome is determined.
BASIS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Intraday touch: any print during the window decides it.
    (
        "intraday_touch",
        re.compile(
            r"\bat any (?:time|point)\b|\bany (?:1|one|five|5|15|30)[\s-]?minute\b"
            r"|\bever (?:reach|hit|exceed|touch)\b|\b(?:reaches|hits|touches|dips)\b"
            r"|\bintraday\b|\b(?:high|low) price\b|\bcandle\b",
            re.I,
        ),
    ),
    # Closing price at a stated moment.
    (
        "close",
        re.compile(
            r"\bclos(?:e|ing|es)\b|\bat (?:the )?(?:end of|close of)\b"
            r"|\bfinal (?:price|value|level)\b|\bsettlement price\b",
            re.I,
        ),
    ),
    # An official result or announcement rather than a price.
    (
        "official_result",
        re.compile(
            r"\bofficial(?:ly)?\b|\bcertified\b|\bannounce[ds]?\b|\bdeclare[ds]?\b"
            r"|\bsworn in\b|\bconfirmed by\b|\bprojected winner\b",
            re.I,
        ),
    ),
    # A published statistic release.
    (
        "data_release",
        re.compile(
            r"\breleased?\b.{0,30}\b(?:report|data|figure|estimate)\b"
            r"|\bfirst (?:release|print|estimate)\b|\brevision\b",
            re.I,
        ),
    ),
]

# Direction of the threshold comparison.
COMPARATOR_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("gte", re.compile(r"\b(?:at or above|greater than or equal|equal to or (?:higher|greater|above))\b", re.I)),
    ("lte", re.compile(r"\b(?:at or below|less than or equal|equal to or (?:lower|less|below))\b", re.I)),
    ("gt", re.compile(r"\b(?:above|greater than|higher than|exceeds?|more than|over)\b", re.I)),
    ("lt", re.compile(r"\b(?:below|less than|lower than|under|dips? to|falls? to)\b", re.I)),
    ("eq", re.compile(r"\bexactly\b|\bequal to\b", re.I)),
]

# Cutoff timestamps: "11:59 PM ET on December 31, 2026".
_TIME_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?\s*"
    r"(?P<tz>et|est|edt|ct|cst|cdt|pt|pst|pdt|gmt|utc)\b",
    re.I,
)

_TZ_CANONICAL = {
    "et": "ET", "est": "ET", "edt": "ET",
    "ct": "CT", "cst": "CT", "cdt": "CT",
    "pt": "PT", "pst": "PT", "pdt": "PT",
    "gmt": "UTC", "utc": "UTC",
}


@dataclass
class ResolutionFacts:
    """What could be determined from one venue's resolution text."""

    source: Optional[str] = None
    basis: Optional[str] = None
    comparator: Optional[str] = None
    thresholds: set[float] = field(default_factory=set)
    cutoff_time: Optional[str] = None  # "23:59 ET"
    expiry: Optional[datetime] = None  # the venue's own resolutionDate
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "basis": self.basis,
            "comparator": self.comparator,
            "thresholds": sorted(self.thresholds),
            "cutoff_time": self.cutoff_time,
            "expiry": self.expiry.isoformat() if self.expiry else None,
        }


def _first_match(patterns: list[tuple[str, re.Pattern[str]]], text: str) -> Optional[str]:
    for name, pattern in patterns:
        if pattern.search(text):
            return name
    return None


def extract_cutoff(text: str) -> Optional[str]:
    """Normalise a stated cutoff to "HH:MM TZ" in 24-hour form."""
    match = _TIME_RE.search(text)
    if not match:
        return None
    try:
        hour = int(match.group("hour"))
    except (TypeError, ValueError):
        return None
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz = _TZ_CANONICAL.get((match.group("tz") or "").lower(), "")
    return f"{hour:02d}:{minute:02d} {tz}".strip()


def extract_facts(market: Market) -> ResolutionFacts:
    """Parse one venue's resolution criteria.

    The title is included because venues split the question between title and
    rules inconsistently — Kalshi often puts the threshold in the title and
    the mechanism in the rules, Polymarket puts both in the description.
    """
    # Basis, source and cutoff come from the rules text only — never the
    # title. Two venues asking a question that *reads* the same is the
    # premise of the problem, not evidence against it: "Will BTC close above
    # $100k" appears verbatim on both venues while one settles on a Binance
    # one-minute low and the other on a Coinbase close. Taking the word
    # "close" out of a shared title as proof of a shared settlement basis
    # would make this module agree with exactly the pairs it exists to catch.
    # No rules text means no verdict, which is what UNVERIFIED is for.
    rules = market.description or ""
    normalised_rules = normalise(rules)

    # Thresholds are the exception: the title is where venues state them
    # unambiguously, while rules text is full of incidental numbers (candle
    # intervals, section references, percentages of a quorum).
    thresholds = significant_numbers(
        market.title,
        market.resolution_date.year if market.resolution_date else None,
    )

    return ResolutionFacts(
        source=_first_match(SOURCE_PATTERNS, rules),
        basis=_first_match(BASIS_PATTERNS, rules),
        comparator=_first_match(COMPARATOR_PATTERNS, normalised_rules),
        thresholds=thresholds,
        cutoff_time=extract_cutoff(rules),
        expiry=market.resolution_date,
        text=rules or market.title,
    )


def _compare_expiry(
    a: Optional[datetime], b: Optional[datetime], tolerance_hours: float
) -> tuple[str, str]:
    if not a or not b:
        return "unknown", "settlement time not published by both venues"
    delta = abs(a - b)
    if delta <= timedelta(hours=tolerance_hours):
        return "match", f"settlement times agree within {delta}"
    return (
        "differ",
        f"settlement times differ by {delta} ({a.isoformat()} vs {b.isoformat()})",
    )


def compare(
    market_a: Market,
    market_b: Market,
    *,
    expiry_tolerance_hours: float = 24.0,
) -> ResolutionAssessment:
    """Assess whether two markets resolve on the same terms.

    Strict by construction. A dimension only counts towards MATCHED when both
    sides produced a value for it; anything else leaves the pair UNVERIFIED.
    Any outright conflict makes it DIFFERS immediately — a single disagreeing
    dimension is enough to make these two different bets.
    """
    facts_a = extract_facts(market_a)
    facts_b = extract_facts(market_b)

    checks: dict[str, str] = {}
    notes: list[str] = []

    # 1. Settlement time — structured, published by both venues, most reliable.
    verdict, note = _compare_expiry(facts_a.expiry, facts_b.expiry, expiry_tolerance_hours)
    checks["settlement_time"] = verdict
    notes.append(note)

    # 2. Basis. The dangerous one: closing price and intraday touch are
    # genuinely different questions that share a title.
    if facts_a.basis and facts_b.basis:
        if facts_a.basis == facts_b.basis:
            checks["basis"] = "match"
            notes.append(f"both settle on {facts_a.basis.replace('_', ' ')}")
        else:
            checks["basis"] = "differ"
            notes.append(
                f"settlement basis differs: {facts_a.basis.replace('_', ' ')} "
                f"vs {facts_b.basis.replace('_', ' ')}"
            )
    else:
        checks["basis"] = "unknown"
        missing = market_a.venue if not facts_a.basis else market_b.venue
        notes.append(f"settlement basis not determinable from {missing} rules")

    # 3. Source. Different price feeds disagree, especially intraday.
    if facts_a.source and facts_b.source:
        if facts_a.source == facts_b.source:
            checks["source"] = "match"
            notes.append(f"both resolve against {facts_a.source}")
        else:
            checks["source"] = "differ"
            notes.append(
                f"resolution source differs: {facts_a.source} vs {facts_b.source}"
            )
    elif facts_a.source or facts_b.source:
        checks["source"] = "unknown"
        notes.append(
            f"only one venue names a source ({facts_a.source or facts_b.source})"
        )
    else:
        checks["source"] = "unknown"
        notes.append("neither venue names a resolution source")

    # 4. Threshold. A different number is a different question, full stop.
    if facts_a.thresholds and facts_b.thresholds:
        if facts_a.thresholds & facts_b.thresholds:
            checks["threshold"] = "match"
            notes.append(
                f"shared threshold {sorted(facts_a.thresholds & facts_b.thresholds)}"
            )
        else:
            checks["threshold"] = "differ"
            notes.append(
                f"thresholds differ: {sorted(facts_a.thresholds)} "
                f"vs {sorted(facts_b.thresholds)}"
            )
    elif facts_a.thresholds or facts_b.thresholds:
        checks["threshold"] = "unknown"
        notes.append("only one venue states a numeric threshold")
    else:
        checks["threshold"] = "n/a"

    # 5. Cutoff time of day, where both state one.
    if facts_a.cutoff_time and facts_b.cutoff_time:
        if facts_a.cutoff_time == facts_b.cutoff_time:
            checks["cutoff"] = "match"
            notes.append(f"both cut off at {facts_a.cutoff_time}")
        else:
            checks["cutoff"] = "differ"
            notes.append(
                f"cutoff differs: {facts_a.cutoff_time} vs {facts_b.cutoff_time}"
            )
    else:
        checks["cutoff"] = "unknown"

    # --- verdict ----------------------------------------------------------
    #
    # Any conflict is disqualifying. Otherwise MATCHED requires positive
    # agreement on the two dimensions that decide the question — when it
    # settles and how — plus no threshold conflict. Absence of evidence stays
    # UNVERIFIED.

    if any(v == "differ" for v in checks.values()):
        status = ResolutionStatus.DIFFERS
    elif (
        checks["settlement_time"] == "match"
        and checks["basis"] == "match"
        and checks["threshold"] in {"match", "n/a"}
    ):
        status = ResolutionStatus.MATCHED
    else:
        status = ResolutionStatus.UNVERIFIED
        notes.append("insufficient evidence to confirm equivalence — not tradeable")

    return ResolutionAssessment(
        status=status,
        resolution_a=(market_a.description or market_a.title)[:4000],
        resolution_b=(market_b.description or market_b.title)[:4000],
        notes=notes,
        checks=checks,
        parsed_a=facts_a.to_dict(),
        parsed_b=facts_b.to_dict(),
    )
