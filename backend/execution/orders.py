"""Order submission and fill tracking.

One rule governs this module: an order is never resubmitted on a timeout. A
request that times out may well have reached the venue, and a blind resend
turns one position into two — which on the second leg of an arbitrage means
an unhedged position in the opposite direction of the one you were worried
about.

When submission is ambiguous, we reconcile against the venue's own view of
open orders and positions rather than guessing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import config
from logging_setup import log
from models import LegStatus, TradeLeg
from venues.adapters import VenueAdapter
from venues.pmxt_client import PmxtError

logger = logging.getLogger(__name__)

# Order statuses the venue considers terminal.
_TERMINAL = {"filled", "canceled", "cancelled", "rejected", "expired"}


@dataclass
class SubmissionResult:
    """What came back from trying to place one order."""

    ok: bool
    order_id: Optional[str] = None
    status: str = "unknown"
    filled_shares: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    error: Optional[str] = None
    ambiguous: bool = False  # submitted but outcome unknown — must reconcile
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


async def submit(
    adapter: VenueAdapter,
    leg: TradeLeg,
    shares: float,
    order_type: str = "market",
    price: Optional[float] = None,
    slippage_pct: Optional[float] = None,
) -> SubmissionResult:
    """Place one order. Never retried here."""
    started = time.perf_counter()
    leg.attempts += 1
    try:
        order = await adapter.create_order(
            market_id=leg.market_id,
            outcome_id=leg.outcome_id,
            side=leg.side,
            shares=shares,
            order_type=order_type,
            price=price,
            slippage_pct=slippage_pct,
            timeout=config.PMXT_ORDER_TIMEOUT_SECONDS,
        )
    except PmxtError as exc:
        latency = (time.perf_counter() - started) * 1000
        # A transport-level failure means we do not know whether the venue
        # received it. Anything else is a clean rejection.
        ambiguous = exc.code in {"SIDECAR_UNAVAILABLE", "TIMEOUT"} or exc.status in {
            408,
            502,
            503,
            504,
        }
        log(
            logger,
            logging.ERROR,
            "order submission failed",
            venue=leg.venue,
            code=exc.code,
            ambiguous=ambiguous,
            latency_ms=round(latency, 1),
        )
        return SubmissionResult(
            ok=False,
            error=f"{exc.code}: {exc}",
            ambiguous=ambiguous,
            latency_ms=latency,
        )

    latency = (time.perf_counter() - started) * 1000
    filled = float(order.get("filled_shares") or 0.0)
    status = str(order.get("status") or "unknown")
    log(
        logger,
        logging.INFO,
        "order submitted",
        venue=leg.venue,
        order_id=order.get("id"),
        status=status,
        shares=shares,
        filled=filled,
        latency_ms=round(latency, 1),
    )
    return SubmissionResult(
        ok=True,
        order_id=str(order.get("id") or ""),
        status=status,
        filled_shares=filled,
        avg_price=float(order.get("price") or 0.0),
        fee=float(order.get("fee") or 0.0),
        latency_ms=latency,
        raw=order,
    )


async def poll_fill(
    adapter: VenueAdapter,
    order_id: str,
    timeout: float,
    interval: float,
) -> dict[str, Any]:
    """Watch an order until it fills, terminates, or the timeout expires.

    Seconds, not scans. The brief is explicit that a single-leg fill must be
    detected within seconds — waiting for the next scheduled scan to notice
    an unhedged position is how a containment problem becomes a loss.
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"id": order_id, "status": "unknown", "filled_shares": 0.0}

    while time.monotonic() < deadline:
        try:
            last = await adapter.fetch_order(order_id)
        except PmxtError as exc:
            log(
                logger,
                logging.WARNING,
                "fill poll failed",
                venue=adapter.venue,
                order_id=order_id,
                code=exc.code,
            )
            await asyncio.sleep(interval)
            continue

        status = str(last.get("status") or "").lower()
        if status in _TERMINAL:
            return last
        if float(last.get("remaining") or 0.0) <= 0 and float(
            last.get("filled_shares") or 0.0
        ) > 0:
            return last
        await asyncio.sleep(interval)

    log(
        logger,
        logging.WARNING,
        "fill poll timed out",
        venue=adapter.venue,
        order_id=order_id,
        last_status=last.get("status"),
    )
    return last


async def cancel_quietly(adapter: VenueAdapter, order_id: str) -> bool:
    """Best-effort cancel. Failure is logged, not raised.

    Called when we are already unwinding; a cancel that fails does not change
    what has to happen next, and the fill reconciliation that follows will
    pick up anything that slipped through.
    """
    if not order_id:
        return False
    try:
        await adapter.cancel_order(order_id)
        return True
    except PmxtError as exc:
        log(
            logger,
            logging.WARNING,
            "cancel failed",
            venue=adapter.venue,
            order_id=order_id,
            code=exc.code,
        )
        return False


async def reconcile_ambiguous(
    adapter: VenueAdapter, leg: TradeLeg
) -> Optional[dict[str, Any]]:
    """Find out whether an order we could not confirm actually landed.

    Checks the venue's position for this outcome. If shares are held that we
    did not know about, the order landed and the leg is filled — which we
    need to know before deciding whether anything must be unwound.
    """
    try:
        positions = await adapter.fetch_positions()
    except PmxtError as exc:
        log(
            logger,
            logging.ERROR,
            "reconciliation failed — exposure state unknown",
            venue=leg.venue,
            outcome_id=leg.outcome_id,
            code=exc.code,
        )
        return None

    for position in positions:
        if position.get("outcome_id") == leg.outcome_id and position.get("size"):
            log(
                logger,
                logging.WARNING,
                "ambiguous order reconciled to a real position",
                venue=leg.venue,
                outcome_id=leg.outcome_id,
                size=position.get("size"),
            )
            return position
    return None


def apply_fill(leg: TradeLeg, order: dict[str, Any]) -> None:
    """Fold a venue order payload into the leg record."""
    filled = float(order.get("filled_shares") or 0.0)
    price = float(order.get("price") or 0.0)
    status = str(order.get("status") or "").lower()

    leg.order_id = str(order.get("id") or leg.order_id or "")
    leg.filled_shares = filled
    leg.avg_fill_price = price or leg.intended_price
    leg.fee = float(order.get("fee") or leg.fee)
    leg.cost = round(filled * leg.avg_fill_price, 6)

    if status == "rejected":
        leg.status = LegStatus.REJECTED
    elif filled <= 0:
        leg.status = LegStatus.UNFILLED
    elif filled + 1e-9 < leg.intended_shares:
        leg.status = LegStatus.PARTIAL
    else:
        leg.status = LegStatus.FILLED
