"""Venue definitions.

One entry per venue we can reach through the self-hosted pmxt sidecar. This
is the only place that knows a venue's credential shape, so adding a venue is
an entry here plus a settings toggle — not a code change anywhere else.

Credential fields name Secret Manager secrets. The values never appear in
this file, in Firestore, or in any API response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CredentialField:
    """One secret a venue needs in order to trade.

    `key` is the field name pmxt expects in its `credentials` object.
    `secret_id` is the Secret Manager secret it is stored under.
    """

    key: str
    secret_id: str
    label: str
    required_for_trading: bool = True
    multiline: bool = False
    help: str = ""


@dataclass(frozen=True)
class VenueSpec:
    name: str
    label: str
    pmxt_exchange: str
    credentials: tuple[CredentialField, ...]
    default_fee_model: dict[str, Any]
    poll_priority: int = 100
    enabled_by_default: bool = False
    # Venue-specific liquidity floors. None means fall back to the global
    # scanning setting.
    default_min_liquidity: float | None = None
    default_min_volume_24h: float | None = None
    # Whether fetchOrderBook works without credentials. Kalshi refuses the
    # book to anonymous callers — verified live against pmxt-core — so an
    # uncredentialed scan gets top-of-book only for that venue.
    public_order_book: bool = True
    notes: str = ""


VENUES: dict[str, VenueSpec] = {
    "polymarket": VenueSpec(
        name="polymarket",
        label="Polymarket",
        pmxt_exchange="polymarket",
        credentials=(
            CredentialField(
                key="privateKey",
                secret_id="predmark-polymarket-private-key",
                label="Private key",
                help="EVM private key used for L1 order signing. This is the funds themselves.",
            ),
            CredentialField(
                key="funderAddress",
                secret_id="predmark-polymarket-proxy-address",
                label="Proxy / funder address",
                required_for_trading=False,
                help="Proxy wallet address funding the trades. Discovered automatically if omitted.",
            ),
        ),
        default_fee_model={"model": "flat_bps", "taker_bps": 0.0, "maker_bps": 0.0},
        poll_priority=10,
        enabled_by_default=True,
        public_order_book=True,
        notes="CLOB on Polygon. Historically no trading fee; confirm the current schedule before funding.",
    ),
    "kalshi": VenueSpec(
        name="kalshi",
        label="Kalshi",
        pmxt_exchange="kalshi",
        credentials=(
            CredentialField(
                key="apiKey",
                secret_id="predmark-kalshi-api-key",
                label="API key ID",
            ),
            CredentialField(
                key="privateKey",
                secret_id="predmark-kalshi-private-key",
                label="RSA private key",
                multiline=True,
                help="PEM block, including the BEGIN/END lines.",
            ),
        ),
        default_fee_model={"model": "kalshi_quadratic", "quadratic_rate": 0.07},
        poll_priority=20,
        enabled_by_default=True,
        public_order_book=False,
        # Kalshi reports liquidity as 0 on every market and its 24h volumes
        # are two to three orders of magnitude below Polymarket's. Applying
        # Polymarket's floor here excludes the venue entirely.
        default_min_liquidity=0.0,
        default_min_volume_24h=100.0,
        notes=(
            "Order book requires credentials — without them the scanner sees "
            "top-of-book only and depth checks are correspondingly weaker. "
            "Reports no liquidity figure, so its floor is applied to 24h volume."
        ),
    ),
    "limitless": VenueSpec(
        name="limitless",
        label="Limitless",
        pmxt_exchange="limitless",
        credentials=(
            CredentialField(
                key="apiKey",
                secret_id="predmark-limitless-api-key",
                label="API key",
            ),
            CredentialField(
                key="privateKey",
                secret_id="predmark-limitless-signing-key",
                label="Signing key",
                help="EIP-712 order signing key.",
            ),
        ),
        default_fee_model={"model": "flat_bps", "taker_bps": 0.0},
        poll_priority=30,
        enabled_by_default=False,
        public_order_book=True,
    ),
}


def venue_spec(name: str) -> VenueSpec:
    try:
        return VENUES[name]
    except KeyError:
        raise ValueError(f"unknown venue: {name}") from None


def credential_fields(name: str) -> tuple[CredentialField, ...]:
    return venue_spec(name).credentials


def secret_id_for(venue: str, key: str) -> str:
    for cred in credential_fields(venue):
        if cred.key == key:
            return cred.secret_id
    raise ValueError(f"unknown credential '{key}' for venue '{venue}'")


def describe_venues() -> list[dict[str, Any]]:
    """Venue metadata for the settings page. No secret values, ever."""
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "pmxt_exchange": spec.pmxt_exchange,
            "public_order_book": spec.public_order_book,
            "notes": spec.notes,
            "credentials": [
                {
                    "key": cred.key,
                    "label": cred.label,
                    "secret_id": cred.secret_id,
                    "required_for_trading": cred.required_for_trading,
                    "multiline": cred.multiline,
                    "help": cred.help,
                }
                for cred in spec.credentials
            ],
        }
        for spec in VENUES.values()
    ]
