"""Per-venue fee models.

A margin quoted before fees is not a margin. Kalshi's fee in particular is
quadratic in price — it peaks at 50c, where most interesting arbitrage sits —
so treating it as a flat percentage understates the cost exactly where it
matters most.

Every model is configurable from the settings page. The defaults in
`venues/registry.py` reflect each venue's published schedule at the time of
writing; they are settings, not facts, and must be confirmed before funding.
"""

from __future__ import annotations

import math

from settings.schema import FeeModel


def kalshi_fee(shares: float, price: float, rate: float = 0.07) -> float:
    """Kalshi's published trading fee, in dollars.

        fee = ceil(rate x C x P x (1 - P))   [rounded up to the cent]

    Quadratic in price: ~0 at the tails, maximum at P = 0.5. At the default
    0.07 rate a 100-share fill at 50c costs $1.75, which is 3.5% of the $50
    notional — enough to erase most cross-venue spreads on its own.
    """
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw_cents = rate * shares * price * (1.0 - price) * 100.0
    # Round before the ceiling. 0.07 * 100 * 0.5 * 0.5 * 100 evaluates to
    # 175.00000000000003 in binary floating point, and an unguarded ceil
    # turns an exact $1.75 into $1.76 — small, but it is a fee model, and a
    # fee model that is wrong by a cent in one direction is wrong.
    return math.ceil(round(raw_cents, 9)) / 100.0


def flat_bps_fee(shares: float, price: float, bps: float) -> float:
    """Basis points of notional (price x shares)."""
    if shares <= 0 or price <= 0 or bps <= 0:
        return 0.0
    return shares * price * (bps / 10_000.0)


def fee_for(
    model: FeeModel,
    shares: float,
    price: float,
    *,
    is_maker: bool = False,
) -> float:
    """Total fee in dollars for filling `shares` at `price` under `model`.

    Includes any fixed per-order cost, so a small trade is correctly shown as
    uneconomic when the venue charges one.
    """
    if shares <= 0:
        return 0.0

    if model.model == "kalshi_quadratic":
        variable = kalshi_fee(shares, price, model.quadratic_rate)
    elif model.model == "flat_bps":
        bps = model.maker_bps if is_maker else model.taker_bps
        variable = flat_bps_fee(shares, price, bps)
    else:
        variable = 0.0

    return round(variable + model.fixed_cost_per_order, 6)
