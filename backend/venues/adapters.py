"""Venue adapters over the pmxt sidecar.

Normalises every venue into the vocabulary in `models.py`: probabilities in
[0, 1], sizes in shares, one market per binary question. Everything above
this layer is venue-agnostic.

Two behaviours here exist because of what live testing showed, not because
the API documents them:

  1. Polymarket's upstream returns HTTP 422 when pmxt forwards `status` or
     `sort`, while a bare `{limit}` fetch succeeds and already comes back
     volume-ordered. So market fetching degrades through a chain of
     progressively simpler parameter sets rather than failing the scan.

  2. Kalshi refuses `fetchOrderBook` to callers without credentials, but its
     market payload carries top-of-book bid/ask with sizes. When the ladder
     is unavailable we synthesise a one-level book and label it
     `depth_source="top_of_book"` so the pre-trade depth check knows exactly
     how much it is actually being told.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from logging_setup import log
from models import BookLevel, Market, OrderBook, Outcome
from venues.pmxt_client import PmxtClient, PmxtError
from venues.registry import VenueSpec, venue_spec

logger = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class VenueAdapter:
    """One venue, normalised.

    Credentials are supplied per call by the caller (from Secret Manager) and
    held only for the lifetime of the adapter instance that a scan or a trade
    creates. Nothing is persisted.
    """

    def __init__(
        self,
        venue: str,
        client: PmxtClient,
        credentials: Optional[dict[str, Any]] = None,
    ) -> None:
        self.venue = venue
        self.spec: VenueSpec = venue_spec(venue)
        self.exchange = self.spec.pmxt_exchange
        self.client = client
        self._credentials = credentials or {}

    @property
    def has_credentials(self) -> bool:
        return bool(self._credentials)

    async def _call(
        self, method: str, args: list[Any], *, authed: bool = False, **kwargs: Any
    ) -> Any:
        return await self.client.call(
            self.exchange,
            method,
            args,
            credentials=self._credentials if authed else None,
            **kwargs,
        )

    # -- market data -------------------------------------------------------

    async def fetch_markets(self, limit: int = 300) -> list[Market]:
        """Pull active markets, degrading through parameter sets on failure.

        Each attempt is strictly simpler than the last. The final attempt is
        the one that is known to work on every venue tested.
        """
        attempts: list[dict[str, Any]] = [
            {"limit": limit, "status": "active", "sort": "volume"},
            {"limit": limit, "status": "active"},
            {"limit": limit},
        ]

        raw: Optional[list[dict[str, Any]]] = None
        last_error: Optional[Exception] = None
        for params in attempts:
            try:
                result = await self._call("fetchMarkets", [params])
            except PmxtError as exc:
                last_error = exc
                continue
            if isinstance(result, list):
                raw = result
                break

        if raw is None:
            log(
                logger,
                logging.ERROR,
                "market fetch failed on every parameter set",
                venue=self.venue,
                error=str(last_error) if last_error else "no data",
            )
            return []

        markets = [m for m in (self._to_market(item) for item in raw) if m]
        # Only ever hand back live markets, whichever path produced them.
        return [m for m in markets if m.status in {"active", "open", ""}]

    def _to_market(self, raw: dict[str, Any]) -> Optional[Market]:
        if not isinstance(raw, dict):
            return None
        outcomes_raw = raw.get("outcomes") or []
        # Binary questions only. Categorical events arrive from both venues
        # already exploded into one binary market per candidate, so this
        # discards nothing we can trade.
        if len(outcomes_raw) != 2:
            return None

        market_id = str(raw.get("marketId") or raw.get("id") or "")
        if not market_id:
            return None

        source_meta = raw.get("sourceMetadata") or {}
        outcomes: list[Outcome] = []
        for index, item in enumerate(outcomes_raw):
            meta = item.get("metadata") or {}
            bid = meta.get("bid")
            ask = meta.get("ask")
            outcomes.append(
                Outcome(
                    outcome_id=str(item.get("outcomeId") or ""),
                    label=str(item.get("label") or ("Yes" if index == 0 else "No")),
                    price=_f(item.get("price")),
                    bid=_f(bid, None) if bid is not None else None,
                    ask=_f(ask, None) if ask is not None else None,
                )
            )

        self._attach_top_of_book_sizes(outcomes, source_meta)

        return Market(
            venue=self.venue,
            market_id=market_id,
            title=str(raw.get("title") or ""),
            description=str(raw.get("description") or ""),
            url=str(raw.get("url") or ""),
            outcomes=outcomes,
            liquidity=_f(raw.get("liquidity")),
            volume_24h=_f(raw.get("volume24h")),
            resolution_date=_parse_dt(raw.get("resolutionDate")),
            status=str(raw.get("status") or "active"),
            category=raw.get("category"),
            tags=[str(t) for t in (raw.get("tags") or [])],
            event_id=str(raw.get("eventId")) if raw.get("eventId") else None,
            tick_size=_f(raw.get("tickSize"), 0.01) or 0.01,
            source_metadata=source_meta if isinstance(source_meta, dict) else {},
        )

    def _attach_top_of_book_sizes(
        self, outcomes: list[Outcome], source_meta: dict[str, Any]
    ) -> None:
        """Fill in bid/ask sizes where the venue publishes them on the market.

        Kalshi quotes a single YES book. Buying NO at price p is the same
        trade as selling YES at 1 - p, so the size available to a NO buyer is
        the size resting on the YES bid — not the YES ask. Getting that
        backwards would overstate depth on exactly the leg we are about to
        take.
        """
        if len(outcomes) != 2:
            return
        yes, no = outcomes

        yes_bid_size = source_meta.get("yes_bid_size_fp")
        yes_ask_size = source_meta.get("yes_ask_size_fp")
        if yes_bid_size is None and yes_ask_size is None:
            return

        yes.bid_size = _f(yes_bid_size, None) if yes_bid_size is not None else None
        yes.ask_size = _f(yes_ask_size, None) if yes_ask_size is not None else None

        # The NO side mirrors: its ask is the complement of the YES bid.
        if yes.bid is not None and no.ask is None:
            no.ask = round(1.0 - yes.bid, 6)
        if yes.ask is not None and no.bid is None:
            no.bid = round(1.0 - yes.ask, 6)
        no.ask_size = yes.bid_size
        no.bid_size = yes.ask_size

    async def fetch_order_book(
        self, market: Market, outcome: Outcome, depth: int = 20
    ) -> OrderBook:
        """Best available depth for one outcome.

        Falls back to a synthesised top-of-book rather than returning
        nothing: a one-level book with an honest label is more useful to the
        depth check than a hole.
        """
        try:
            raw = await self._call(
                "fetchOrderBook",
                [outcome.outcome_id, depth],
                authed=self.has_credentials,
            )
        except PmxtError as exc:
            if exc.code not in {"AUTHENTICATION_ERROR", "NOT_SUPPORTED"}:
                log(
                    logger,
                    logging.INFO,
                    "order book unavailable, using top of book",
                    venue=self.venue,
                    code=exc.code,
                )
            return self._synthetic_book(outcome)

        if not isinstance(raw, dict):
            return self._synthetic_book(outcome)

        bids = [
            BookLevel(price=_f(level.get("price")), size=_f(level.get("size")))
            for level in (raw.get("bids") or [])
            if _f(level.get("size")) > 0
        ]
        asks = [
            BookLevel(price=_f(level.get("price")), size=_f(level.get("size")))
            for level in (raw.get("asks") or [])
            if _f(level.get("size")) > 0
        ]
        if not bids and not asks:
            return self._synthetic_book(outcome)

        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        return OrderBook(
            outcome_id=outcome.outcome_id,
            bids=bids,
            asks=asks,
            depth_source="book",
        )

    def _synthetic_book(self, outcome: Outcome) -> OrderBook:
        bids: list[BookLevel] = []
        asks: list[BookLevel] = []
        if outcome.bid is not None and outcome.bid > 0:
            bids.append(BookLevel(price=outcome.bid, size=_f(outcome.bid_size)))
        if outcome.ask is not None and outcome.ask > 0:
            asks.append(BookLevel(price=outcome.ask, size=_f(outcome.ask_size)))
        return OrderBook(
            outcome_id=outcome.outcome_id,
            bids=bids,
            asks=asks,
            depth_source="top_of_book" if (bids or asks) else "none",
        )

    # -- account -----------------------------------------------------------

    async def fetch_balance(self) -> dict[str, float]:
        """Available and total balance in USD terms."""
        raw = await self._call("fetchBalance", [], authed=True)
        entries = raw if isinstance(raw, list) else [raw] if raw else []
        available = sum(_f(e.get("available")) for e in entries if isinstance(e, dict))
        total = sum(_f(e.get("total")) for e in entries if isinstance(e, dict))
        locked = sum(_f(e.get("locked")) for e in entries if isinstance(e, dict))
        return {"available": available, "total": total, "locked": locked}

    async def fetch_positions(self) -> list[dict[str, Any]]:
        raw = await self._call("fetchPositions", [], authed=True)
        if not isinstance(raw, list):
            return []
        return [
            {
                "market_id": str(p.get("marketId") or ""),
                "outcome_id": str(p.get("outcomeId") or ""),
                "outcome_label": p.get("outcomeLabel"),
                "size": _f(p.get("size")),
                "entry_price": _f(p.get("entryPrice")),
                "current_price": _f(p.get("currentPrice")),
                "current_value": _f(p.get("currentValue")),
                "unrealised_pnl": _f(p.get("unrealizedPnL")),
                "realised_pnl": _f(p.get("realizedPnL")),
                "venue": self.venue,
            }
            for p in raw
            if isinstance(p, dict)
        ]

    # -- trading -----------------------------------------------------------

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
        """Place one order. Never retried by this layer.

        A timeout on order submission is ambiguous — the venue may have taken
        it. Resolution belongs to the execution layer, which reconciles
        against open orders rather than guessing.
        """
        params: dict[str, Any] = {
            "marketId": market_id,
            "outcomeId": outcome_id,
            "side": side,
            "type": order_type,
            "amount": shares,
            "denom": "shares",
        }
        if price is not None:
            params["price"] = price
        if order_type == "market" and slippage_pct is not None:
            params["slippage_pct"] = slippage_pct

        raw = await self._call(
            "createOrder",
            [params],
            authed=True,
            timeout=timeout,
            retries=0,
        )
        return self._to_order(raw)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        raw = await self._call("cancelOrder", [order_id], authed=True, retries=0)
        return self._to_order(raw)

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        raw = await self._call("fetchOrder", [order_id], authed=True, retries=1)
        return self._to_order(raw)

    def _to_order(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"id": "", "status": "unknown", "filled_shares": 0.0}

        # `filled` is USDC-denominated for buys on some venues; filledShares
        # is authoritative when present.
        filled_shares = raw.get("filledShares")
        filled = _f(filled_shares) if filled_shares is not None else _f(raw.get("filled"))
        price = _f(raw.get("price"))
        return {
            "id": str(raw.get("id") or ""),
            "market_id": str(raw.get("marketId") or ""),
            "outcome_id": str(raw.get("outcomeId") or ""),
            "side": str(raw.get("side") or ""),
            "type": str(raw.get("type") or ""),
            "status": str(raw.get("status") or "unknown"),
            "price": price,
            "amount": _f(raw.get("amount")),
            "filled_shares": filled,
            "remaining": _f(raw.get("remaining")),
            "fee": _f(raw.get("fee")),
            "fee_rate_bps": _f(raw.get("feeRateBps")),
            "timestamp": raw.get("timestamp"),
        }
