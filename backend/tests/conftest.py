"""Test fixtures.

The fakes here are test doubles, not product behaviour. Nothing in the
service simulates a venue: `settings.execution.dry_run` places no orders and
records no fills. These doubles exist so the execution paths that must not be
exercised against real money — single-leg fill, failed unwind, ambiguous
submission — can be exercised at all.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

# Tests import the backend modules the same way the container does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import BookLevel, Market, OrderBook, Outcome  # noqa: E402
from settings.schema import (  # noqa: E402
    ExecutionSettings,
    FeeModel,
    RiskSettings,
    Settings,
    VenueSettings,
)


def make_market(
    venue: str = "polymarket",
    market_id: str = "m1",
    title: str = "Will Bitcoin close above $100,000 on December 31, 2026?",
    description: str = "",
    yes_price: float = 0.45,
    liquidity: float = 50_000.0,
    resolution_date: Optional[datetime] = None,
    yes_label: str = "Yes",
    no_label: str = "No",
) -> Market:
    return Market(
        venue=venue,
        market_id=market_id,
        title=title,
        description=description,
        url=f"https://{venue}.example/{market_id}",
        outcomes=[
            Outcome(
                outcome_id=f"{market_id}-yes",
                label=yes_label,
                price=yes_price,
                bid=round(yes_price - 0.01, 4),
                ask=round(yes_price + 0.01, 4),
            ),
            Outcome(
                outcome_id=f"{market_id}-no",
                label=no_label,
                price=round(1 - yes_price, 4),
                bid=round(1 - yes_price - 0.01, 4),
                ask=round(1 - yes_price + 0.01, 4),
            ),
        ],
        liquidity=liquidity,
        volume_24h=liquidity,
        resolution_date=resolution_date
        or datetime(2027, 1, 1, 5, 0, tzinfo=timezone.utc),
    )


def make_book(
    outcome_id: str = "o1",
    ask_price: float = 0.45,
    ask_size: float = 1000.0,
    bid_price: Optional[float] = None,
    bid_size: float = 1000.0,
    depth_source: str = "book",
    levels: int = 3,
) -> OrderBook:
    """A ladder that thickens as it gets worse, like a real one."""
    asks = [
        BookLevel(price=round(ask_price + i * 0.01, 4), size=ask_size * (i + 1))
        for i in range(levels)
    ]
    bid = bid_price if bid_price is not None else round(ask_price - 0.02, 4)
    bids = [
        BookLevel(price=round(bid - i * 0.01, 4), size=bid_size * (i + 1))
        for i in range(levels)
    ]
    return OrderBook(
        outcome_id=outcome_id,
        bids=bids,
        asks=asks,
        depth_source=depth_source,
    )


def make_settings(**overrides: Any) -> Settings:
    settings = Settings(
        venues={
            "polymarket": VenueSettings(
                enabled=True,
                pmxt_exchange="polymarket",
                label="Polymarket",
                fee_model=FeeModel(model="none"),
                max_exposure=1000.0,
            ),
            "kalshi": VenueSettings(
                enabled=True,
                pmxt_exchange="kalshi",
                label="Kalshi",
                fee_model=FeeModel(model="kalshi_quadratic", quadratic_rate=0.07),
                max_exposure=1000.0,
            ),
        },
        execution=ExecutionSettings(
            trading_enabled=True,
            dry_run=False,
            stake_per_trade=100.0,
            max_stake_per_trade=1000.0,
            fill_timeout_seconds=1.0,
            fill_poll_interval_seconds=0.1,
            second_leg_retry_limit=1,
            unwind_retry_limit=2,
        ),
        risk=RiskSettings(max_total_exposure=10_000.0, daily_loss_limit=1000.0),
    )
    for section, values in overrides.items():
        current = getattr(settings, section)
        for key, value in values.items():
            setattr(current, key, value)
    return settings


# --------------------------------------------------------------------------
# Fake venue adapter
# --------------------------------------------------------------------------


@dataclass
class FakeOrder:
    id: str
    side: str
    shares: float
    filled: float
    price: float
    status: str


@dataclass
class FakeAdapter:
    """A venue that behaves exactly as scripted.

    `fill_ratio` controls how much of each buy fills: 1.0 fills completely,
    0.0 not at all, 0.5 half. `sell_fails` makes every unwind attempt fail,
    which is how the EXPOSED path gets tested.
    """

    venue: str = "polymarket"
    fill_ratio: float = 1.0
    fill_price: Optional[float] = None
    sell_fails: bool = False
    sell_fill_ratio: float = 1.0
    sell_price_factor: float = 0.95  # unwind at a worse price than entry
    raise_on_buy: Optional[Exception] = None
    orders: list[FakeOrder] = field(default_factory=list)
    positions_response: list[dict[str, Any]] = field(default_factory=list)
    buy_calls: int = 0
    sell_calls: int = 0
    _counter: int = 0

    @property
    def has_credentials(self) -> bool:
        return True

    async def create_order(
        self,
        market_id: str,
        outcome_id: str,
        side: str,
        shares: float,
        order_type: str = "market",
        price: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        self._counter += 1
        order_id = f"{self.venue}-{self._counter}"

        if side == "buy":
            self.buy_calls += 1
            if self.raise_on_buy:
                raise self.raise_on_buy
            filled = shares * self.fill_ratio
            fill_price = self.fill_price if self.fill_price is not None else (price or 0.5)
        else:
            self.sell_calls += 1
            if self.sell_fails:
                from venues.pmxt_client import PmxtError

                raise PmxtError(
                    "venue rejected the sell", code="ORDER_REJECTED", exchange=self.venue
                )
            filled = shares * self.sell_fill_ratio
            base = self.fill_price if self.fill_price is not None else (price or 0.5)
            fill_price = round(base * self.sell_price_factor, 6)

        status = "filled" if filled >= shares - 1e-9 else ("open" if filled > 0 else "open")
        order = FakeOrder(
            id=order_id,
            side=side,
            shares=shares,
            filled=filled,
            price=fill_price,
            status=status,
        )
        self.orders.append(order)
        return {
            "id": order_id,
            "market_id": market_id,
            "outcome_id": outcome_id,
            "side": side,
            "type": order_type,
            "status": status,
            "price": fill_price,
            "amount": shares,
            "filled_shares": filled,
            "remaining": max(0.0, shares - filled),
            "fee": 0.0,
        }

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        order = next((o for o in self.orders if o.id == order_id), None)
        if not order:
            return {"id": order_id, "status": "unknown", "filled_shares": 0.0}
        return {
            "id": order.id,
            "status": "filled" if order.filled >= order.shares - 1e-9 else "open",
            "price": order.price,
            "amount": order.shares,
            "filled_shares": order.filled,
            "remaining": max(0.0, order.shares - order.filled),
            "fee": 0.0,
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "canceled", "filled_shares": 0.0}

    async def fetch_positions(self) -> list[dict[str, Any]]:
        return self.positions_response

    async def fetch_balance(self) -> dict[str, float]:
        return {"available": 10_000.0, "total": 10_000.0, "locked": 0.0}


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def future_date() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=30)
