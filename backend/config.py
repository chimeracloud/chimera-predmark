"""Process configuration.

Only infrastructure wiring lives here — the things that identify *where* the
service runs. Everything that is a trading decision lives in Firestore and is
edited from the settings page (see ``settings/schema.py``). If a value in this
file needs changing, that is a redeploy; that is the line.

No credential ever appears here. Venue credentials are read from Secret
Manager at execution time, never from the environment.
"""

from __future__ import annotations

import os

# --- Google Cloud ---------------------------------------------------------

PROJECT_ID: str = os.getenv("GCP_PROJECT", "chiops")
REGION: str = os.getenv("GCP_REGION", "europe-west1")

# Raw scan payloads. One object per scan, partitioned by date.
GCS_BUCKET: str = os.getenv("GCS_BUCKET", "predmark-data")
GCS_SCAN_PREFIX: str = os.getenv("GCS_SCAN_PREFIX", "scans")

# Firestore. `predmark-config` and `predmark-trades` are modelled as
# collections in the project's default database — see README, "Firestore
# layout". Override FIRESTORE_DATABASE if Charles provisions a named database.
FIRESTORE_DATABASE: str = os.getenv("FIRESTORE_DATABASE", "(default)")
CONFIG_COLLECTION: str = os.getenv("CONFIG_COLLECTION", "predmark-config")
TRADES_COLLECTION: str = os.getenv("TRADES_COLLECTION", "predmark-trades")

# Documents within CONFIG_COLLECTION.
SETTINGS_DOC = "settings"
STATE_DOC = "state"
AUDIT_SUBCOLLECTION = "audit"
OPPORTUNITIES_COLLECTION = "predmark-opportunities"

# --- pmxt sidecar ---------------------------------------------------------
#
# Self-hosted pmxt-core, started by the container entrypoint alongside
# uvicorn. Never pmxt.dev: the hosted service is not in the request path, and
# the `router` pseudo-venue (the only part of pmxt-core that calls home) is
# refused by venues/pmxt_client.py.

PMXT_URL: str = os.getenv("PMXT_URL", "http://127.0.0.1:3847")
# .strip() is not cosmetic: a secret stored with a trailing newline —
# which `openssl rand | gcloud --data-file=-` produces by default —
# makes an illegal HTTP header value, and every sidecar call fails with
# a bare LocalProtocolError that says nothing about the cause.
PMXT_ACCESS_TOKEN: str = os.getenv("PMXT_ACCESS_TOKEN", "").strip()
PMXT_TIMEOUT_SECONDS: float = float(os.getenv("PMXT_TIMEOUT_SECONDS", "60"))
PMXT_ORDER_TIMEOUT_SECONDS: float = float(os.getenv("PMXT_ORDER_TIMEOUT_SECONDS", "25"))

# --- service --------------------------------------------------------------

PORT: int = int(os.getenv("PORT", "8080"))
SERVICE_NAME: str = os.getenv("K_SERVICE", "predmark")
REVISION: str = os.getenv("K_REVISION", "local")
BUILD_SHA: str = os.getenv("BUILD_SHA", "unknown")

# Browser origins permitted to call the API. The dashboard is served from
# Cloudflare Pages and reaches Cloud Run through the portal proxy
# (CHI-POL-004), so this is a secondary control, not the primary one.
ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://predmark.pages.dev,http://localhost:8788",
    ).split(",")
    if o.strip()
]

# Concurrency ceiling for outbound venue calls in a single scan. Keeps us
# well inside venue rate limits and bounds the scan's memory profile.
VENUE_CONCURRENCY: int = int(os.getenv("VENUE_CONCURRENCY", "8"))
