"""Depth-aware, fee-aware margin.

The arbitrage: buy `shares` of one outcome on venue A and `shares` of the
complementary outcome on venue B. Exactly one side pays $1 per share at
resolution. So the trade is profitable when

    shares x $1  >  cost(leg A) + fees(leg A) + cost(leg B) + fees(leg B)

Two things separate this from the naive version that adds two top-of-book
prices together:

  * Size is priced against the ladder, not the top. Taking 500 shares walks
    through several levels and the VWAP is worse than the best ask. A margin
    computed at the top of book is a margin available for one share.

  * Fees are charged on the fill, per venue, under that venue's own model.

`best_execution` searches for the size that maximises profit rather than
assuming the configured stake is achievable — a spread that supports $8 of
stake at 3% is a real opportunity, and rejecting it because the stake is set
to $10 would be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from margin.fees import fee_for
from models import LegQuote, Market, OrderBook, Outcome
from settings.schema import FeeModel


@dataclass
class Fill:
    """The result of walking a book to fill a size."""

    shares: float  # shares actually obtainable
    cost: float  # before fees
    avg_price: float
    best_price: float
    depth_available: float  # total shares on the ladder
    complete: bool  # whether the full request was fillable


def walk_book(book: OrderBook, shares: float, side: str = "buy") -> Fill:
    """Consume the ladder to fill `shares`, returning the true VWAP.

    Buying eats asks from the best price upward; selling eats bids from the
    best price downward. A partially fillable request returns what the book
    can actually provide, with `complete=False`.
    """
    levels = book.asks if side == "buy" else book.bids
    depth = sum(level.size for level in levels)
    if shares <= 0 or not levels:
        return Fill(0.0, 0.0, 0.0, 0.0, depth, False)

    remaining = shares
    cost = 0.0
    taken = 0.0
    for level in levels:
        if remaining <= 0:
            break
        take = min(level.size, remaining)
        cost += take * level.price
        taken += take
        remaining -= take

    avg = cost / taken if taken > 0 else 0.0
    return Fill(
        shares=taken,
        cost=cost,
        avg_price=avg,
        best_price=levels[0].price,
        depth_available=depth,
        complete=remaining <= 1e-9,
    )


def quote_leg(
    market: Market,
    outcome: Outcome,
    book: OrderBook,
    shares: float,
    fee_model: FeeModel,
    side: str = "buy",
) -> LegQuote:
    """Price one leg at a given size, including fees."""
    fill = walk_book(book, shares, side)
    fee = fee_for(fee_model, fill.shares, fill.avg_price)
    return LegQuote(
        venue=market.venue,
        market_id=market.market_id,
        outcome_id=outcome.outcome_id,
        outcome_label=outcome.label,
        market_title=market.title,
        side=side,
        avg_price=round(fill.avg_price, 6),
        best_price=round(fill.best_price, 6),
        shares=round(fill.shares, 6),
        cost=round(fill.cost, 6),
        fee=round(fee, 6),
        depth_available=round(fill.depth_available, 6),
        depth_source=book.depth_source,
    )


@dataclass
class MarginResult:
    shares: float
    gross_cost: float
    total_fees: float
    total_cost: float
    payout: float
    profit: float
    net_margin: float  # profit / total_cost
    leg_a: LegQuote
    leg_b: LegQuote
    complete: bool  # both legs fillable at the requested size


def evaluate(
    market_a: Market,
    outcome_a: Outcome,
    book_a: OrderBook,
    fees_a: FeeModel,
    market_b: Market,
    outcome_b: Outcome,
    book_b: OrderBook,
    fees_b: FeeModel,
    shares: float,
) -> MarginResult:
    """Price both legs at `shares` and return the net margin.

    Both legs are quoted at the *same* share count deliberately. Buying
    unequal sizes leaves the difference as a naked directional position,
    which is the thing this strategy exists to avoid.
    """
    quote_a = quote_leg(market_a, outcome_a, book_a, shares, fees_a)
    quote_b = quote_leg(market_b, outcome_b, book_b, shares, fees_b)

    # The tradeable size is whatever both books can support.
    matched = min(quote_a.shares, quote_b.shares)
    if matched < shares - 1e-9 and matched > 0:
        quote_a = quote_leg(market_a, outcome_a, book_a, matched, fees_a)
        quote_b = quote_leg(market_b, outcome_b, book_b, matched, fees_b)

    gross = quote_a.cost + quote_b.cost
    fees = quote_a.fee + quote_b.fee
    total = gross + fees
    payout = matched  # $1 per share, one side always pays
    profit = payout - total
    margin = profit / total if total > 0 else 0.0

    return MarginResult(
        shares=round(matched, 6),
        gross_cost=round(gross, 6),
        total_fees=round(fees, 6),
        total_cost=round(total, 6),
        payout=round(payout, 6),
        profit=round(profit, 6),
        net_margin=round(margin, 6),
        leg_a=quote_a,
        leg_b=quote_b,
        complete=matched >= shares - 1e-9 and matched > 0,
    )


def headline_margin(
    book_a: OrderBook,
    fees_a: FeeModel,
    book_b: OrderBook,
    fees_b: FeeModel,
) -> float:
    """Net margin for a single share at the top of both books.

    Used to decide whether a pair is worth pricing properly. Cheap, and an
    upper bound on what any larger size can achieve.
    """
    ask_a, ask_b = book_a.best_ask, book_b.best_ask
    if not ask_a or not ask_b:
        return -1.0
    cost = ask_a + ask_b
    cost += fee_for(fees_a, 1.0, ask_a) + fee_for(fees_b, 1.0, ask_b)
    if cost <= 0:
        return -1.0
    return round((1.0 - cost) / cost, 6)


def headline_margin_from_prices(
    ask_a: float | None,
    fees_a: FeeModel,
    ask_b: float | None,
    fees_b: FeeModel,
) -> float:
    """Top-of-book margin from prices alone, before any book is fetched.

    The scan uses this to pick which pairs justify the cost of a book fetch.
    """
    if not ask_a or not ask_b:
        return -1.0
    cost = ask_a + ask_b
    cost += fee_for(fees_a, 1.0, ask_a) + fee_for(fees_b, 1.0, ask_b)
    if cost <= 0:
        return -1.0
    return round((1.0 - cost) / cost, 6)


def best_execution(
    market_a: Market,
    outcome_a: Outcome,
    book_a: OrderBook,
    fees_a: FeeModel,
    market_b: Market,
    outcome_b: Outcome,
    book_b: OrderBook,
    fees_b: FeeModel,
    max_stake: float,
    min_margin: float,
) -> MarginResult | None:
    """Largest size, up to `max_stake` of capital, that still clears `min_margin`.

    Walks down from the stake-implied size. Because VWAP worsens monotonically
    with size, the first size that clears the floor is also the largest one
    that does.
    """
    ask_a, ask_b = book_a.best_ask, book_b.best_ask
    if not ask_a or not ask_b:
        return None

    # Shares affordable at top-of-book prices; real prices are worse, so this
    # is a ceiling, not a target.
    per_share = ask_a + ask_b
    if per_share <= 0:
        return None
    ceiling = max_stake / per_share

    depth_ceiling = min(
        sum(level.size for level in book_a.asks),
        sum(level.size for level in book_b.asks),
    )
    ceiling = min(ceiling, depth_ceiling)
    if ceiling <= 0:
        return None

    best: MarginResult | None = None
    # Geometric ladder: full size first, then progressively smaller. Twelve
    # steps covers three orders of magnitude, which is more than the range
    # between a $10 stake and a venue minimum.
    size = ceiling
    for _ in range(12):
        if size < 1e-6:
            break
        result = evaluate(
            market_a,
            outcome_a,
            book_a,
            fees_a,
            market_b,
            outcome_b,
            book_b,
            fees_b,
            size,
        )
        if result.shares > 0:
            if best is None or result.profit > best.profit:
                best = result
            if result.net_margin >= min_margin:
                return result
        size *= 0.7

    return best
