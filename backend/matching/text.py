"""Text normalisation and similarity.

The Jaccard/Levenshtein blend is adapted from realfishsam's
prediction-market-arbitrage-bot (MIT, see NOTICE). What is added here is the
prediction-market-specific part: venue title conventions differ enough that
raw string similarity pairs the wrong markets. "Will BTC close above $100,000
on Dec 31?" and "Bitcoin above $100k on Dec 31" are the same question;
"Bitcoin above $100k" and "Bitcoin above $120k" are not, and score higher on
raw similarity than the first pair does.

So numbers and dates are extracted and compared exactly, separately from the
fuzzy title score. A number mismatch is disqualifying regardless of how
similar the prose is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Words that carry no discriminating signal in a market title.
STOPWORDS = {
    "will",
    "the",
    "a",
    "an",
    "be",
    "is",
    "are",
    "was",
    "were",
    "to",
    "of",
    "in",
    "on",
    "at",
    "by",
    "for",
    "and",
    "or",
    "if",
    "it",
    "this",
    "that",
    "with",
    "as",
    "from",
    "market",
    "resolve",
    "resolves",
    "question",
    "yes",
    "no",
    "not",
}

# Venue-specific phrasing that means the same thing.
SYNONYMS = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "doge": "dogecoin",
    "xrp": "ripple",
    "potus": "president",
    "gop": "republican",
    "dems": "democrat",
    "democrats": "democrat",
    "democratic": "democrat",
    "republicans": "republican",
    "fed": "federal reserve",
    "prez": "president",
    "vs": "versus",
    "v": "versus",
}

_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "bn": 1_000_000_000}

_NUMBER_RE = re.compile(
    r"(?<![\w.])(\$?)(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|bn|b|%)?(?![\w])",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_DATE_RES = [
    re.compile(
        r"\b(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
        r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?P<year>\d{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
        r"\.?,?\s*(?P<year>\d{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?P<year>20\d{2})-(?P<month_n>\d{2})-(?P<day>\d{2})\b"),
]


def question_part(title: str) -> str:
    """The tradeable question, with any event prefix removed.

    Polymarket titles are compound — "Next Prime Minister of Sweden - Will
    Ebba Busch be the next Prime Minister of Sweden?" — while Kalshi states
    the question alone. Comparing the compound form against the bare one
    dilutes the score with duplicated context and depresses genuine matches.

    Only the last segment is taken, and only when it is substantial enough to
    stand as a question on its own; some venues use hyphens inside a single
    question ("Fed decision - September") and truncating those would lose the
    subject.
    """
    if " - " not in title:
        return title
    head, _, tail = title.rpartition(" - ")
    tail = tail.strip()
    if len(tail) < 15 or len(tail.split()) < 3:
        return title
    return tail


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, expand synonyms."""
    text = text.lower().strip()
    text = re.sub(r"[‘’“”]", "'", text)
    text = re.sub(r"[^\w\s$%.,:-]", " ", text)
    tokens = []
    for token in text.split():
        token = token.strip(".,:-'")
        if not token:
            continue
        tokens.append(SYNONYMS.get(token, token))
    return " ".join(tokens)


def tokens(text: str) -> set[str]:
    """Content words, stopwords removed."""
    return {t for t in normalise(text).split() if t and t not in STOPWORDS}


def parse_number(raw: str, suffix: str = "") -> Optional[float]:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    suffix = suffix.lower()
    if suffix in _MULTIPLIERS:
        value *= _MULTIPLIERS[suffix]
    return value


def extract_numbers(text: str) -> set[float]:
    """Numeric quantities in a title — thresholds, prices, percentages.

    "$100k", "100,000" and "100000" all normalise to the same value, so the
    same threshold expressed in two venue conventions compares equal.
    """
    found: set[float] = set()
    for match in _NUMBER_RE.finditer(text):
        _, raw, suffix = match.groups()
        suffix = suffix or ""
        if suffix == "%":
            value = parse_number(raw)
            if value is not None:
                found.add(round(value, 4))
            continue
        value = parse_number(raw, suffix)
        if value is not None:
            found.add(round(value, 4))
    return found


def extract_dates(text: str, default_year: Optional[int] = None) -> set[str]:
    """Calendar dates, normalised to YYYY-MM-DD.

    A year absent from the text is filled from `default_year` (the market's
    resolution date) — venues routinely omit it when it is obvious.
    """
    found: set[str] = set()
    for pattern in _DATE_RES:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            if groups.get("month_n"):
                month = int(groups["month_n"])
            else:
                month = _MONTHS.get((groups.get("month") or "").lower()[:4].rstrip("."), 0)
                if not month:
                    month = _MONTHS.get((groups.get("month") or "").lower()[:3], 0)
            if not month:
                continue
            try:
                day = int(groups["day"])
            except (TypeError, ValueError):
                continue
            year_raw = groups.get("year")
            year = int(year_raw) if year_raw else default_year
            if not year:
                continue
            try:
                found.add(datetime(year, month, day).strftime("%Y-%m-%d"))
            except ValueError:
                continue
    return found


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def levenshtein_ratio(a: str, b: str) -> float:
    """1 - normalised edit distance. Two-row DP; titles are short."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) < len(b):
        a, b = b, a

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        previous = current
    return 1.0 - previous[-1] / max(len(a), len(b))


def _score(a: str, b: str) -> float:
    na, nb = normalise(a), normalise(b)
    return round(jaccard(tokens(a), tokens(b)) * 0.6 + levenshtein_ratio(na, nb) * 0.4, 6)


def similarity(a: str, b: str) -> float:
    """Blended title similarity in [0, 1].

    Jaccard dominates because word overlap survives the reordering venues do
    to each other's phrasing; edit distance breaks ties and rescues titles
    that differ only in inflection.

    Scored on both the full titles and the bare questions, taking the better.
    A Polymarket compound title scores poorly against a Kalshi bare question
    in full form and well once the event prefix is dropped, and there is no
    reason to penalise a real match for a venue's formatting convention.
    """
    best = _score(a, b)
    qa, qb = question_part(a), question_part(b)
    if qa != a or qb != b:
        best = max(best, _score(qa, qb))
    return best


# Capitalised words that carry no identifying weight in a market title.
# Without this list every "Will ... Prime Minister ..." title shares two
# proper nouns and the entity check would wave through Sweden against Israel.
_GENERIC_CAPITALISED = {
    "will", "the", "a", "an", "who", "what", "when", "next", "new", "first",
    "last", "before", "after", "by", "in", "on", "at", "of", "to", "and", "or",
    "prime", "minister", "president", "presidential", "election", "elections",
    "nominee", "nomination", "party", "leader", "chair", "chairman", "cup",
    "world", "national", "international", "league", "championship", "final",
    "finals", "game", "match", "season", "year", "month", "day", "week",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
    "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
    "oct", "october", "nov", "november", "dec", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "yes", "no", "not", "up", "down", "above", "below", "over", "under",
    "us", "u.s.", "usa", "vs", "versus",
}


def entities(text: str) -> set[str]:
    """Distinctive proper nouns — the subject of the question.

    "Will Ebba Busch be the next Prime Minister of Sweden?" yields
    {ebba, busch, sweden}; the Israeli equivalent yields {naftali, bennett,
    israel}. Disjoint, so the two are not the same question however similar
    the prose. Generic title furniture is excluded, or every question about a
    prime minister would look like every other.
    """
    found: set[str] = set()
    for raw in re.findall(r"\b[A-Z][\w'’.-]{1,}\b", text):
        token = raw.lower().strip(".'-")
        if not token or token in _GENERIC_CAPITALISED or token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        found.add(SYNONYMS.get(token, token))
    return found


def significant_numbers(text: str, default_year: Optional[int] = None) -> set[float]:
    """Numbers that are thresholds, with calendar components removed.

    "Will Bitcoin close above $100,000 on December 31, 2026?" yields the raw
    set {31, 2026, 100000}. Comparing that against the $120,000 version of
    the same title finds {31, 2026} in common and concludes the two markets
    agree — which is how you end up pairing two completely different bets.
    The day and year belong to the date, not to the threshold, so they are
    stripped before any threshold comparison.
    """
    numbers = extract_numbers(text)
    if not numbers:
        return numbers

    excluded: set[float] = set()
    for iso in extract_dates(text, default_year):
        year, month, day = (float(part) for part in iso.split("-"))
        excluded |= {year, month, day}
    # Bare years mentioned without a full date ("the 2028 election").
    excluded |= {n for n in numbers if 1900 <= n <= 2200 and n == int(n)}

    return numbers - excluded


@dataclass
class TitleFacts:
    """The structured content of a title, for exact comparison."""

    numbers: set[float] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)


def title_facts(text: str, default_year: Optional[int] = None) -> TitleFacts:
    return TitleFacts(
        numbers=significant_numbers(text, default_year),
        dates=extract_dates(text, default_year),
        tokens=tokens(text),
    )
