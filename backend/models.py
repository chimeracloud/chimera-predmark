"""Domain model.

Deliberately plain dataclasses rather than ORM objects: everything here is
either serialised to Firestore/GCS as a dict or rendered as JSON, and a scan
builds tens of thousands of these. `to_dict` is explicit so the wire format
never drifts silently when a field is renamed.

Money is always USD. Prices are always probabilities in [0, 1] — never cents.
Sizes are always shares (contracts), and a share pays $1 on resolution. That
convention is enforced at the adapter boundary so nothing downstream has to
ask which unit it is holding.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------
# Resolution equivalence
# --------------------------------------------------------------------------


class ResolutionStatus(str, Enum):
    """How confident we are that two venues are asking the same question.

    UNVERIFIED is the default and the only honest answer when the comparison
    could not reach a conclusion. Execution requires MATCHED.
    """

    MATCHED = "MATCHED"
    DIFFERS = "DIFFERS"
    UNVERIFIED = "UNVERIFIED"


@dataclass
class ResolutionAssessment:
    status: ResolutionStatus = ResolutionStatus.UNVERIFIED
    resolution_a: str = ""
    resolution_b: str = ""
    notes: list[str] = field(default_factory=list)
    # Per-dimension verdicts: "match" | "differ" | "unknown".
    checks: dict[str, str] = field(default_factory=dict)
    # What each side was parsed as, for the dashboard to show side by side.
    parsed_a: dict[str, Any] = field(default_factory=dict)
    parsed_b: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "resolution_a": self.resolution_a,
            "resolution_b": self.resolution_b,
            "resolution_notes": " | ".join(self.notes),
            "notes": list(self.notes),
            "checks": dict(self.checks),
            "parsed_a": dict(self.parsed_a),
            "parsed_b": dict(self.parsed_b),
        }


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


@dataclass
class BookLevel:
    price: float
    size: float  # shares available at this price

    def to_dict(self) -> dict[str, Any]:
        return {"price": self.price, "size": self.size}


@dataclass
class OrderBook:
    """One outcome's book.

    `depth_source` records how the ladder was obtained, because it changes
    what a depth check is worth:

      "book"          full ladder from the venue
      "top_of_book"   best bid/ask with size, synthesised from market metadata
                      (Kalshi returns this without credentials; the real
                      ladder needs an API key)
      "none"          no depth information at all
    """

    outcome_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    depth_source: str = "none"
    fetched_at: datetime = field(default_factory=utcnow)

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "bids": [level.to_dict() for level in self.bids[:20]],
            "asks": [level.to_dict() for level in self.asks[:20]],
            "depth_source": self.depth_source,
            "fetched_at": _iso(self.fetched_at),
        }


@dataclass
class Outcome:
    outcome_id: str
    label: str
    price: float  # venue's headline probability, not necessarily executable
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Market:
    """A binary tradeable question on one venue, normalised."""

    venue: str
    market_id: str
    title: str
    description: str  # the venue's resolution text — the input to matching
    url: str
    outcomes: list[Outcome]
    liquidity: float = 0.0
    volume_24h: float = 0.0
    resolution_date: Optional[datetime] = None
    status: str = "active"
    category: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    event_id: Optional[str] = None
    tick_size: float = 0.01
    source_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def yes(self) -> Optional[Outcome]:
        return self.outcomes[0] if self.outcomes else None

    @property
    def no(self) -> Optional[Outcome]:
        return self.outcomes[1] if len(self.outcomes) > 1 else None

    @property
    def effective_liquidity(self) -> float:
        """Liquidity for filtering purposes.

        Kalshi reports `liquidity` as 0 on every market, so a naive floor
        would silently exclude the entire venue. Fall back to 24h volume,
        which every venue does populate.
        """
        return self.liquidity if self.liquidity > 0 else self.volume_24h

    def outcome_by_id(self, outcome_id: str) -> Optional[Outcome]:
        return next((o for o in self.outcomes if o.outcome_id == outcome_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "market_id": self.market_id,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "liquidity": self.liquidity,
            "volume_24h": self.volume_24h,
            "resolution_date": _iso(self.resolution_date),
            "status": self.status,
            "category": self.category,
            "tags": list(self.tags),
            "event_id": self.event_id,
            "tick_size": self.tick_size,
        }


# --------------------------------------------------------------------------
# Opportunities
# --------------------------------------------------------------------------


@dataclass
class LegQuote:
    """One side of a proposed trade, priced against real depth."""

    venue: str
    market_id: str
    outcome_id: str
    outcome_label: str
    market_title: str
    side: str = "buy"
    # VWAP across the levels consumed to fill `shares`.
    avg_price: float = 0.0
    best_price: float = 0.0
    shares: float = 0.0
    cost: float = 0.0  # shares * avg_price, before fees
    fee: float = 0.0
    depth_available: float = 0.0
    depth_source: str = "none"

    @property
    def total_cost(self) -> float:
        return self.cost + self.fee

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "total_cost": self.total_cost}


@dataclass
class Opportunity:
    """A priced, size-aware, resolution-assessed arbitrage candidate.

    The invariant that makes this an arbitrage: buying `shares` of both legs
    means exactly one leg pays $1 per share at resolution, whatever happens.
    Profit is that payout minus what both legs cost including fees.
    """

    id: str = field(default_factory=lambda: new_id("opp"))
    scan_id: str = ""
    detected_at: datetime = field(default_factory=utcnow)

    leg_a: Optional[LegQuote] = None
    leg_b: Optional[LegQuote] = None

    shares: float = 0.0
    gross_cost: float = 0.0  # both legs, before fees
    total_fees: float = 0.0
    total_cost: float = 0.0  # both legs, after fees
    payout: float = 0.0  # shares * $1
    profit: float = 0.0  # payout - total_cost
    net_margin: float = 0.0  # profit / total_cost, as a fraction

    # Margin at the top of book for one share, before size is considered.
    headline_margin: float = 0.0

    match_score: float = 0.0
    match_notes: list[str] = field(default_factory=list)
    resolution: ResolutionAssessment = field(default_factory=ResolutionAssessment)

    tradeable: bool = False
    blocked_reasons: list[str] = field(default_factory=list)

    @property
    def venues(self) -> list[str]:
        return [leg.venue for leg in (self.leg_a, self.leg_b) if leg]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scan_id": self.scan_id,
            "detected_at": _iso(self.detected_at),
            "leg_a": self.leg_a.to_dict() if self.leg_a else None,
            "leg_b": self.leg_b.to_dict() if self.leg_b else None,
            "shares": self.shares,
            "gross_cost": self.gross_cost,
            "total_fees": self.total_fees,
            "total_cost": self.total_cost,
            "payout": self.payout,
            "profit": self.profit,
            "net_margin": self.net_margin,
            "headline_margin": self.headline_margin,
            "match_score": self.match_score,
            "match_notes": list(self.match_notes),
            "resolution": self.resolution.to_dict(),
            "resolution_status": self.resolution.status.value,
            "tradeable": self.tradeable,
            "blocked_reasons": list(self.blocked_reasons),
            "venues": self.venues,
        }


# --------------------------------------------------------------------------
# Trades
# --------------------------------------------------------------------------


class LegStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    REJECTED = "REJECTED"
    UNWOUND = "UNWOUND"
    UNWIND_FAILED = "UNWIND_FAILED"


class TradeStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"  # both legs filled, held to resolution
    CONTAINED = "CONTAINED"  # single-leg fill, successfully unwound
    EXPOSED = "EXPOSED"  # single-leg fill, unwind failed — needs a human
    SETTLED = "SETTLED"
    FAILED = "FAILED"  # nothing filled
    CANCELLED = "CANCELLED"


@dataclass
class TradeLeg:
    venue: str
    market_id: str
    outcome_id: str
    outcome_label: str
    market_title: str
    side: str
    intended_shares: float
    intended_price: float
    order_id: Optional[str] = None
    status: LegStatus = LegStatus.PENDING
    filled_shares: float = 0.0
    avg_fill_price: float = 0.0
    fee: float = 0.0
    cost: float = 0.0
    submitted_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    attempts: int = 0
    error: Optional[str] = None
    # Set when this leg had to be sold back out.
    unwind_order_id: Optional[str] = None
    unwind_proceeds: float = 0.0
    unwind_shares: float = 0.0

    @property
    def is_filled(self) -> bool:
        return self.filled_shares > 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["submitted_at"] = _iso(self.submitted_at)
        data["settled_at"] = _iso(self.settled_at)
        return data


@dataclass
class Trade:
    id: str = field(default_factory=lambda: new_id("trd"))
    opportunity_id: str = ""
    scan_id: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    status: TradeStatus = TradeStatus.PENDING
    legs: list[TradeLeg] = field(default_factory=list)

    intended_shares: float = 0.0
    intended_cost: float = 0.0
    expected_profit: float = 0.0
    expected_margin: float = 0.0

    # Realised figures, filled in as the trade progresses.
    actual_cost: float = 0.0
    realised_pnl: Optional[float] = None
    settlement_notes: str = ""

    # The execution-quality numbers that actually matter.
    unmatched_leg: bool = False
    containment_cost: Optional[float] = None
    unwind_attempted: bool = False
    unwind_succeeded: Optional[bool] = None

    resolution_status: str = ResolutionStatus.UNVERIFIED.value
    resolution_override: bool = False
    dry_run: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, **detail: Any) -> None:
        self.events.append(
            {"at": utcnow().isoformat(), "event": event, "detail": detail}
        )
        self.updated_at = utcnow()

    @property
    def venues(self) -> list[str]:
        return [leg.venue for leg in self.legs]

    @property
    def filled_legs(self) -> list[TradeLeg]:
        return [leg for leg in self.legs if leg.is_filled]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "opportunity_id": self.opportunity_id,
            "scan_id": self.scan_id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "status": self.status.value,
            "legs": [leg.to_dict() for leg in self.legs],
            "intended_shares": self.intended_shares,
            "intended_cost": self.intended_cost,
            "expected_profit": self.expected_profit,
            "expected_margin": self.expected_margin,
            "actual_cost": self.actual_cost,
            "realised_pnl": self.realised_pnl,
            "settlement_notes": self.settlement_notes,
            "unmatched_leg": self.unmatched_leg,
            "containment_cost": self.containment_cost,
            "unwind_attempted": self.unwind_attempted,
            "unwind_succeeded": self.unwind_succeeded,
            "resolution_status": self.resolution_status,
            "resolution_override": self.resolution_override,
            "dry_run": self.dry_run,
            "events": list(self.events),
            "venues": self.venues,
        }
