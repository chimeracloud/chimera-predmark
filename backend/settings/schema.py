"""The settings model.

Every number in this file is editable from the settings page and lives in
Firestore. Nothing here requires a redeploy to change — that is the whole
point of the settings page.

Defaults are deliberately conservative: trading off, small stake, high margin
floor, MATCHED resolution required. A fresh deployment scans and records but
does not trade until Charles turns it on.

Fee defaults are documented per venue in `VenueSettings.fee_model`. They must
be confirmed against each venue's published schedule before capital moves —
a wrong fee model does not fail loudly, it just quietly turns a positive
margin into a negative one.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

FeeModelName = Literal["none", "flat_bps", "kalshi_quadratic"]


class FeeModel(BaseModel):
    """How a venue charges for a fill.

    none              no trading fee
    flat_bps          `taker_bps`/10000 of notional (price * shares)
    kalshi_quadratic  Kalshi's published schedule:
                      fee = ceil(rate * shares * price * (1 - price)) in cents
    """

    model: FeeModelName = "none"
    taker_bps: float = Field(default=0.0, ge=0, le=1000)
    maker_bps: float = Field(default=0.0, ge=0, le=1000)
    # Coefficient for kalshi_quadratic. Kalshi publishes 0.07 for general
    # markets and 0.035 for selected series.
    quadratic_rate: float = Field(default=0.07, ge=0, le=1)
    # Any flat per-order cost (gas, relayer, withdrawal amortisation).
    fixed_cost_per_order: float = Field(default=0.0, ge=0)


class VenueSettings(BaseModel):
    enabled: bool = False
    # pmxt exchange identifier — the {exchange} in POST /api/{exchange}/{method}.
    pmxt_exchange: str
    label: str
    fee_model: FeeModel = Field(default_factory=FeeModel)
    # Lower number polls first when the scan is rate limited.
    poll_priority: int = Field(default=100, ge=1, le=1000)
    # How many markets to pull per scan.
    market_limit: int = Field(default=300, ge=1, le=2000)
    # Cap on capital deployed to this venue at any one time.
    max_exposure: float = Field(default=100.0, ge=0)
    # Per-venue liquidity floors, overriding the global scanning values when
    # set. Venues report on entirely different scales: Polymarket publishes
    # real liquidity in the millions, while Kalshi reports `liquidity: 0` on
    # every market and its whole active book turned over $2.6k in the 24h
    # window this was written against. One global floor either excludes
    # Kalshi completely or lets Polymarket's dust through.
    min_liquidity: Optional[float] = Field(default=None, ge=0)
    min_volume_24h: Optional[float] = Field(default=None, ge=0)
    # Venue order type. Market orders take liquidity immediately, which is
    # what an arbitrage needs; limit orders risk resting unfilled and are the
    # main way a hedged trade becomes an unhedged one.
    order_type: Literal["market", "limit"] = "market"
    # Passed to pmxt for market orders on hosted-style venues.
    slippage_pct: float = Field(default=2.0, ge=0, le=100)


class ScanningSettings(BaseModel):
    # Informational: the true cadence is the Cloud Scheduler job. Surfaced so
    # the dashboard can show the next expected scan.
    poll_interval_seconds: int = Field(default=300, ge=30, le=86400)
    # Global fallbacks. A venue's own floor takes precedence where set.
    min_liquidity: float = Field(default=1000.0, ge=0)
    min_volume_24h: float = Field(default=0.0, ge=0)
    # Only pair markets resolving at least this far out. A market resolving in
    # minutes cannot be entered on two venues safely.
    min_hours_to_resolution: float = Field(default=1.0, ge=0)
    # Three years, not one. Prediction markets are heavily long-dated — the
    # liquid Kalshi book at the time of writing is largely 2028 elections and
    # the 2030 World Cup, and a 365-day cap excluded roughly nine tenths of
    # it. Capital lock-up is a real cost, but it is a cost to weigh in the
    # margin floor, not a reason to be blind to the market.
    max_days_to_resolution: float = Field(default=1095.0, ge=0)
    # Title-similarity floor for a pair to be considered at all.
    match_threshold: float = Field(default=0.72, ge=0, le=1)
    # Categories to include; empty means all.
    include_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    # Substring filters applied to titles.
    exclude_keywords: list[str] = Field(default_factory=list)
    # Order books are fetched only for pairs whose top-of-book margin clears
    # this, because a book fetch per pair per scan is the expensive part.
    book_fetch_margin_floor: float = Field(default=-0.02)
    max_book_fetches: int = Field(default=120, ge=0, le=2000)


class MarginSettings(BaseModel):
    # Net of both venues' fees. A pair below this is recorded but never traded.
    min_margin_to_trade: float = Field(default=0.02, ge=0, le=1)
    # Below this a pair is not even written down.
    min_margin_to_record: float = Field(default=0.0, ge=-1, le=1)
    # Ignore books where an outcome trades below this — the extreme tails are
    # where stale quotes and phantom liquidity live.
    min_outcome_price: float = Field(default=0.02, ge=0, le=0.5)
    max_outcome_price: float = Field(default=0.98, ge=0.5, le=1)

    # A binary market's two outcomes must price to about $1 between them —
    # that is arithmetic, not a market view, since holding both pays exactly
    # $1. A venue whose own YES and NO do not sum to 1 is publishing data we
    # cannot use. Observed live: Limitless returned the identical order book
    # for both outcomes of several markets, so YES and NO both quoted 0.044
    # and the pair showed a 1090% margin that does not exist.
    complement_tolerance: float = Field(default=0.06, ge=0.005, le=0.5)

    # Real cross-venue spreads run to a few per cent. Anything far above that
    # is a data error — a stale quote, a mis-mapped outcome, a venue serving
    # the wrong book — and treating it as an opportunity is how a bad feed
    # becomes a loss. Recorded and displayed, never traded.
    max_plausible_margin: float = Field(default=0.35, ge=0.01, le=10)


class ExecutionSettings(BaseModel):
    trading_enabled: bool = False
    dry_run: bool = True
    stake_per_trade: float = Field(default=10.0, ge=0)
    # Hard ceiling. The UI refuses a stake above this and so does the API.
    max_stake_per_trade: float = Field(default=100.0, ge=0)
    max_concurrent_trades: int = Field(default=1, ge=0, le=50)
    # How far the second leg may chase before we give up and unwind.
    slippage_tolerance: float = Field(default=0.01, ge=0, le=0.5)
    second_leg_retry_limit: int = Field(default=2, ge=0, le=10)
    second_leg_reprice_ceiling: float = Field(default=0.03, ge=0, le=0.5)
    # How long to wait for a fill before treating a leg as unfilled.
    fill_timeout_seconds: float = Field(default=8.0, ge=1, le=120)
    fill_poll_interval_seconds: float = Field(default=0.75, ge=0.1, le=10)
    # Unwind is a market sell; this is how much worse than entry we accept
    # before flagging the unwind as a bad one (it still executes — an
    # unhedged position is worse than a bad price).
    unwind_max_loss: float = Field(default=0.10, ge=0, le=1)
    unwind_retry_limit: int = Field(default=3, ge=0, le=10)
    # Require the full intended size to be available on both books.
    require_full_depth: bool = True


class RiskSettings(BaseModel):
    kill_switch: bool = False
    kill_switch_reason: str = ""
    daily_loss_limit: float = Field(default=50.0, ge=0)
    daily_trade_limit: int = Field(default=10, ge=0, le=1000)
    max_total_exposure: float = Field(default=200.0, ge=0)
    required_resolution_status: Literal["MATCHED", "MATCHED_OR_UNVERIFIED"] = "MATCHED"
    # Trading an UNVERIFIED pair is holding two different bets, not a hedge.
    # This override is explicit, logged, and off.
    allow_unverified_override: bool = False
    override_reason: str = ""


class AlertSettings(BaseModel):
    unmatched_legs_threshold: int = Field(default=1, ge=0)
    failed_unwinds_threshold: int = Field(default=1, ge=0)
    errors_threshold: int = Field(default=10, ge=0)
    # Halt trading automatically when unmatched legs pile up. The execution
    # layer failing is the one thing that must not be allowed to continue.
    halt_on_unmatched_legs: int = Field(default=3, ge=0)


class Settings(BaseModel):
    version: int = 1
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    venues: dict[str, VenueSettings] = Field(default_factory=dict)
    scanning: ScanningSettings = Field(default_factory=ScanningSettings)
    margin: MarginSettings = Field(default_factory=MarginSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)

    @field_validator("venues")
    @classmethod
    def _no_unknown_venues(
        cls, value: dict[str, VenueSettings]
    ) -> dict[str, VenueSettings]:
        from venues.registry import VENUES

        unknown = set(value) - set(VENUES)
        if unknown:
            raise ValueError(f"unknown venues: {sorted(unknown)}")
        return value

    def enabled_venues(self) -> list[str]:
        return sorted(
            (name for name, v in self.venues.items() if v.enabled),
            key=lambda name: self.venues[name].poll_priority,
        )

    def effective_stake(self) -> float:
        return min(self.execution.stake_per_trade, self.execution.max_stake_per_trade)


def default_settings() -> Settings:
    """The shape a fresh deployment starts with.

    Trading off, dry run on, one venue pair's worth of stake. Charles turns
    things on from the settings page once credentials are in place.
    """
    from venues.registry import VENUES

    return Settings(
        venues={
            name: VenueSettings(
                enabled=spec.enabled_by_default,
                pmxt_exchange=spec.pmxt_exchange,
                label=spec.label,
                fee_model=FeeModel(**spec.default_fee_model),
                poll_priority=spec.poll_priority,
                min_liquidity=spec.default_min_liquidity,
                min_volume_24h=spec.default_min_volume_24h,
            )
            for name, spec in VENUES.items()
        }
    )


def merge_settings(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a partial settings update over the current document.

    The settings page sends only the section it changed; a shallow update
    would silently blank the rest.
    """
    merged = dict(current)
    for key, value in patch.items():
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
            # Venue maps are merged per venue, not replaced wholesale.
            and key not in {"__replace__"}
        ):
            merged[key] = merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


def diff_settings(
    before: dict[str, Any], after: dict[str, Any], path: str = ""
) -> list[dict[str, Any]]:
    """Field-level changes between two settings documents, for the audit log.

    "Settings changes are how trading systems break, and the record of them is
    how you find out why" — so the record is per field, not per document.
    """
    changes: list[dict[str, Any]] = []
    keys = set(before) | set(after)
    for key in sorted(keys):
        if key in {"updated_at", "updated_by", "version"}:
            continue
        here = f"{path}.{key}" if path else key
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(diff_settings(old, new, here))
        elif old != new:
            changes.append({"field": here, "from": old, "to": new})
    return changes
