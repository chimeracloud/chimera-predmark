"""Exposure and balances across venues.

The rule that shapes this module: positions cannot be netted across
platforms. A YES on Polymarket and a NO on Kalshi are two separate holdings,
each with its own collateral sitting on its own venue. Exposure is therefore
the sum of both legs, never the difference, and capital has to be present on
both venues at once for a trade to be possible at all.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from logging_setup import log
from settings import secrets
from settings.schema import Settings
from storage import trades as trade_store
from venues.adapters import VenueAdapter
from venues.pmxt_client import PmxtClient, PmxtError

logger = logging.getLogger(__name__)


async def fetch_balances(
    client: PmxtClient, settings: Settings, venues: Optional[list[str]] = None
) -> dict[str, dict[str, float]]:
    """Available balance per venue, fetched concurrently.

    A venue that cannot be reached returns no entry rather than a zero — the
    pre-trade check treats "unknown" as blocking, which is the safe reading.
    A zero would look like a definite answer.
    """
    names = venues if venues is not None else settings.enabled_venues()

    async def one(venue: str) -> tuple[str, Optional[dict[str, float]]]:
        credentials = secrets.load_credentials(venue)
        if not credentials:
            return venue, None
        adapter = VenueAdapter(venue, client, credentials)
        try:
            return venue, await adapter.fetch_balance()
        except PmxtError as exc:
            log(
                logger,
                logging.WARNING,
                "balance fetch failed",
                venue=venue,
                code=exc.code,
            )
            return venue, None

    results = await asyncio.gather(*(one(v) for v in names), return_exceptions=True)
    balances: dict[str, dict[str, float]] = {}
    for result in results:
        if isinstance(result, BaseException):
            continue
        venue, balance = result
        if balance is not None:
            balances[venue] = balance
    return balances


async def fetch_positions(
    client: PmxtClient, settings: Settings
) -> list[dict[str, Any]]:
    """Live positions per venue, straight from the venues themselves."""

    async def one(venue: str) -> list[dict[str, Any]]:
        credentials = secrets.load_credentials(venue)
        if not credentials:
            return []
        adapter = VenueAdapter(venue, client, credentials)
        try:
            return await adapter.fetch_positions()
        except PmxtError as exc:
            log(
                logger,
                logging.WARNING,
                "position fetch failed",
                venue=venue,
                code=exc.code,
            )
            return []

    gathered = await asyncio.gather(
        *(one(v) for v in settings.enabled_venues()), return_exceptions=True
    )
    positions: list[dict[str, Any]] = []
    for result in gathered:
        if isinstance(result, list):
            positions.extend(result)
    return positions


def committed_capital() -> dict[str, float]:
    """What our own trade records say is committed, per venue."""
    return trade_store.exposure_by_venue()


def exposure_report(
    balances: dict[str, dict[str, float]], settings: Settings
) -> dict[str, Any]:
    """Per-venue exposure against caps, plus headroom.

    Headroom is the binding constraint of the three: the venue cap, the
    global cap, and the cash actually available on that venue.
    """
    committed = committed_capital()
    total_committed = sum(committed.values())
    global_headroom = max(0.0, settings.risk.max_total_exposure - total_committed)

    venues: dict[str, Any] = {}
    for name, venue_settings in settings.venues.items():
        if not venue_settings.enabled:
            continue
        used = committed.get(name, 0.0)
        balance = balances.get(name)
        available = balance.get("available") if balance else None
        cap_headroom = max(0.0, venue_settings.max_exposure - used)
        headroom = min(
            cap_headroom,
            global_headroom,
            available if available is not None else cap_headroom,
        )
        venues[name] = {
            "venue": name,
            "label": venue_settings.label,
            "committed": round(used, 6),
            "max_exposure": venue_settings.max_exposure,
            "cap_headroom": round(cap_headroom, 6),
            "balance": balance,
            "headroom": round(headroom, 6),
            "balance_known": balance is not None,
        }

    return {
        "venues": venues,
        "total_committed": round(total_committed, 6),
        "max_total_exposure": settings.risk.max_total_exposure,
        "global_headroom": round(global_headroom, 6),
    }
