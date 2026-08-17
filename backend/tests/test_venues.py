"""Venue adapter tests.

The payload shapes below were captured from the self-hosted pmxt sidecar
running against the live venues, so the normalisation is tested against what
the venues actually send rather than what the docs say they send.
"""

from __future__ import annotations

import pytest

from venues.adapters import VenueAdapter
from venues.pmxt_client import PmxtClient, PmxtError

# Captured from POST /api/kalshi/fetchMarkets. Kalshi quotes a single YES
# book and reports liquidity as 0 on every market.
KALSHI_MARKET = {
    "id": "KXELONMARS-99",
    "marketId": "KXELONMARS-99",
    "eventId": "KXELONMARS-99",
    "title": "Will Elon Musk visit Mars before Aug 1, 2099?",
    "description": "If Elon Musk visits Mars before the earlier of his death or Aug 1, 2099, then the market resolves to Yes.",
    "outcomes": [
        {
            "outcomeId": "KXELONMARS-99",
            "label": "Mars",
            "price": 0.12,
            "metadata": {"bid": 0.11, "ask": 0.12},
        },
        {
            "outcomeId": "KXELONMARS-99-NO",
            "label": "Not Mars",
            "price": 0.885,
            "metadata": {"bid": 0.88, "ask": 0.89},
        },
    ],
    "resolutionDate": "2099-08-08T15:00:00.000Z",
    "volume24h": 1,
    "volume": 117135.08,
    "liquidity": 0,
    "url": "https://kalshi.com/events/KXELONMARS-99",
    "status": "active",
    "sourceMetadata": {
        "yes_ask_size_fp": "327.88",
        "yes_bid_size_fp": "40.00",
        "close_time": "2099-08-01T04:59:00Z",
        "series_ticker": "KXELONMARS",
    },
}

# Captured from POST /api/polymarket/fetchMarkets.
POLYMARKET_MARKET = {
    "id": "2463838",
    "marketId": "2463838",
    "title": "Will Bitcoin dip to $60,000 by December 31, 2026?",
    "description": "This market will resolve to Yes if any Binance 1 minute candle for BTC/USDT has a final Low price equal to or lower than $60,000.",
    "outcomes": [
        {"outcomeId": "1126779", "label": "↓ 60,000", "price": 0.35, "metadata": {}},
        {"outcomeId": "9803446", "label": "Not ↓ 60,000", "price": 0.65, "metadata": {}},
    ],
    "resolutionDate": "2027-01-01T05:00:00.000Z",
    "volume24h": 1200.0,
    "liquidity": 98262.09,
    "url": "https://polymarket.com/event/x",
    "status": "active",
    "tickSize": 0.01,
}


class StubClient(PmxtClient):
    """A pmxt client that answers from a script instead of the network."""

    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__(base_url="http://stub", access_token="t")
        self.responses = responses
        self.calls: list[tuple[str, str, list]] = []

    async def call(self, exchange, method, args=None, credentials=None, **kwargs):
        self.calls.append((exchange, method, args or []))
        response = self.responses.get(method)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(args or [])
        return response


@pytest.mark.asyncio
async def test_kalshi_market_normalises():
    client = StubClient({"fetchMarkets": [KALSHI_MARKET]})
    markets = await VenueAdapter("kalshi", client).fetch_markets()

    assert len(markets) == 1
    market = markets[0]
    assert market.venue == "kalshi"
    assert market.market_id == "KXELONMARS-99"
    assert market.yes.price == pytest.approx(0.12)
    assert market.resolution_date.year == 2099
    # The rules text is preserved verbatim — the resolution engine reads it.
    assert "visits Mars" in market.description


def test_kalshi_liquidity_falls_back_to_volume():
    """Kalshi reports liquidity as 0 on every market.

    A naive liquidity floor would silently exclude the entire venue, which
    would look like "no opportunities" rather than "a filter is wrong".
    """
    from venues.adapters import VenueAdapter as _  # noqa: F401
    from models import Market, Outcome

    market = Market(
        venue="kalshi", market_id="k", title="t", description="", url="",
        outcomes=[Outcome("a", "Yes", 0.5), Outcome("b", "No", 0.5)],
        liquidity=0.0, volume_24h=25_000.0,
    )
    assert market.effective_liquidity == 25_000.0


@pytest.mark.asyncio
async def test_kalshi_no_side_depth_mirrors_the_yes_bid():
    """Buying NO consumes the YES bid, not the YES ask.

    Kalshi quotes one book. Buying NO at price p is selling YES at 1 - p, so
    the size available to a NO buyer is what rests on the YES bid. Reading
    the ask instead would overstate depth on exactly the leg being taken —
    here by 8x.
    """
    client = StubClient({"fetchMarkets": [KALSHI_MARKET]})
    markets = await VenueAdapter("kalshi", client).fetch_markets()
    yes, no = markets[0].outcomes

    assert yes.ask_size == pytest.approx(327.88)
    assert yes.bid_size == pytest.approx(40.00)
    # The NO buyer can only get what the YES bid holds.
    assert no.ask_size == pytest.approx(40.00)
    assert no.bid_size == pytest.approx(327.88)


@pytest.mark.asyncio
async def test_market_fetch_degrades_through_parameter_sets():
    """Polymarket's upstream 422s on `status` and `sort` but serves a bare fetch.

    Verified live. The scan must degrade rather than lose the venue.
    """
    attempts: list[dict] = []

    def responder(args):
        params = args[0] if args else {}
        attempts.append(params)
        if "status" in params or "sort" in params:
            raise PmxtError("Request failed with status code 422", status=422)
        return [POLYMARKET_MARKET]

    client = StubClient({"fetchMarkets": responder})
    markets = await VenueAdapter("polymarket", client).fetch_markets(limit=50)

    assert len(markets) == 1
    assert len(attempts) == 3  # tried the rich sets first
    assert "status" not in attempts[-1] and "sort" not in attempts[-1]


@pytest.mark.asyncio
async def test_total_market_fetch_failure_returns_empty_not_an_exception():
    """One dead venue must not fail the whole scan."""
    client = StubClient({"fetchMarkets": PmxtError("venue down", status=500)})
    assert await VenueAdapter("polymarket", client).fetch_markets() == []


@pytest.mark.asyncio
async def test_non_binary_markets_are_skipped():
    three_way = dict(POLYMARKET_MARKET)
    three_way["outcomes"] = POLYMARKET_MARKET["outcomes"] + [
        {"outcomeId": "x", "label": "Draw", "price": 0.1, "metadata": {}}
    ]
    client = StubClient({"fetchMarkets": [three_way]})
    assert await VenueAdapter("polymarket", client).fetch_markets() == []


@pytest.mark.asyncio
async def test_order_book_falls_back_to_top_of_book_when_auth_is_refused():
    """Kalshi refuses the ladder without credentials — verified live.

    A one-level book with an honest label is more useful to the depth check
    than a hole, provided the label is honest.
    """
    client = StubClient(
        {
            "fetchMarkets": [KALSHI_MARKET],
            "fetchOrderBook": PmxtError(
                "Trading operations require authentication",
                code="AUTHENTICATION_ERROR",
                status=401,
            ),
        }
    )
    adapter = VenueAdapter("kalshi", client)
    markets = await adapter.fetch_markets()
    book = await adapter.fetch_order_book(markets[0], markets[0].outcomes[0])

    assert book.depth_source == "top_of_book"
    assert book.best_ask == pytest.approx(0.12)
    assert book.asks[0].size == pytest.approx(327.88)


@pytest.mark.asyncio
async def test_order_book_ladder_is_sorted_correctly():
    client = StubClient(
        {
            "fetchMarkets": [POLYMARKET_MARKET],
            "fetchOrderBook": {
                "bids": [
                    {"price": 0.33, "size": 100},
                    {"price": 0.37, "size": 50},
                    {"price": 0.35, "size": 75},
                ],
                "asks": [
                    {"price": 0.42, "size": 100},
                    {"price": 0.38, "size": 50},
                    {"price": 0.0, "size": 0},  # dropped: no size
                ],
            },
        }
    )
    adapter = VenueAdapter("polymarket", client)
    markets = await adapter.fetch_markets()
    book = await adapter.fetch_order_book(markets[0], markets[0].outcomes[0])

    assert book.depth_source == "book"
    assert book.best_bid == pytest.approx(0.37)  # highest bid first
    assert book.best_ask == pytest.approx(0.38)  # lowest ask first
    assert all(level.size > 0 for level in book.asks)


@pytest.mark.asyncio
async def test_the_hosted_router_is_refused():
    """Self-hosted by requirement: `router` is the one exchange that calls home."""
    client = PmxtClient(base_url="http://stub", access_token="t")
    with pytest.raises(PmxtError) as caught:
        await client.call("router", "fetchArbitrage")
    assert caught.value.code == "FORBIDDEN_EXCHANGE"
    assert "pmxt.dev" in str(caught.value)


@pytest.mark.asyncio
async def test_order_normalisation_prefers_filled_shares_over_usdc_filled():
    """`filled` is USDC-denominated for buys on some venues; shares are not."""
    client = StubClient(
        {
            "createOrder": {
                "id": "o1",
                "status": "filled",
                "price": 0.45,
                "amount": 100,
                "filled": 45.0,  # dollars
                "filledShares": 100.0,  # shares — authoritative
                "remaining": 0,
            }
        }
    )
    order = await VenueAdapter("polymarket", client, {"privateKey": "x"}).create_order(
        "m1", "o1", "buy", 100
    )
    assert order["filled_shares"] == pytest.approx(100.0)
