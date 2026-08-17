"""Raw scan data in GCS.

Every scan writes one object holding everything it saw — markets counted,
pairs considered, margins computed, resolution verdicts, and why each
opportunity was or was not tradeable. Firestore holds the current view; this
is the record you go back to when a trade looks wrong three weeks later, and
it is what the history view aggregates.

Layout: gs://predmark-data/scans/YYYY/MM/DD/<scan_id>.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import config
from logging_setup import log, redact_value

logger = logging.getLogger(__name__)

_client = None


def client():  # pragma: no cover - thin wrapper over the GCP client
    global _client
    if _client is None:
        from google.cloud import storage

        _client = storage.Client(project=config.PROJECT_ID)
    return _client


def scan_path(scan_id: str, at: Optional[datetime] = None) -> str:
    at = at or datetime.now(timezone.utc)
    return (
        f"{config.GCS_SCAN_PREFIX}/{at:%Y/%m/%d}/{scan_id}.json"
    )


def write_scan(scan_id: str, payload: dict[str, Any]) -> Optional[str]:
    """Persist a scan. Returns the gs:// URI, or None if the write failed.

    A failed write is logged and swallowed: losing the archive copy of a scan
    is bad, but it is not a reason to abandon a scan that has already found
    tradeable opportunities.
    """
    path = scan_path(scan_id, payload.get("_at"))
    try:
        bucket = client().bucket(config.GCS_BUCKET)
        blob = bucket.blob(path)
        blob.upload_from_string(
            json.dumps(redact_value(payload), default=str, separators=(",", ":")),
            content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001 - archival must not break the scan
        log(logger, logging.ERROR, "scan archive write failed", error=str(exc), path=path)
        return None

    uri = f"gs://{config.GCS_BUCKET}/{path}"
    log(logger, logging.INFO, "scan archived", uri=uri, scan_id=scan_id)
    return uri


def list_recent_scans(limit: int = 50) -> list[dict[str, Any]]:
    """Recent scan objects, newest first — used by the history view."""
    try:
        blobs = client().list_blobs(
            config.GCS_BUCKET, prefix=f"{config.GCS_SCAN_PREFIX}/"
        )
        entries = [
            {
                "path": blob.name,
                "uri": f"gs://{config.GCS_BUCKET}/{blob.name}",
                "size": blob.size,
                "created": blob.time_created.isoformat() if blob.time_created else None,
            }
            for blob in blobs
        ]
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "scan listing failed", error=str(exc))
        return []

    entries.sort(key=lambda e: e["created"] or "", reverse=True)
    return entries[:limit]


def read_scan(path: str) -> Optional[dict[str, Any]]:
    try:
        blob = client().bucket(config.GCS_BUCKET).blob(path)
        return json.loads(blob.download_as_text())
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "scan read failed", error=str(exc), path=path)
        return None
