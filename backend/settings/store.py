"""Settings and runtime state in Firestore.

Two documents in `predmark-config`:

    settings   the configuration the settings page edits
    state      counters the service maintains — daily P&L, trade counts,
               last scan, unmatched-leg tally

Every settings write records a field-level audit entry in the `audit`
subcollection: what changed, from what, to what, when, by whom. Settings
changes are how trading systems break and the record of them is how you find
out why.

The kill switch is read from Firestore on every execution decision rather
than cached in the process. That is what makes it work without a redeploy or
a restart: flipping it in the UI stops the next trade, not the next
deployment.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import config
from logging_setup import log
from settings.schema import Settings, default_settings, diff_settings, merge_settings

logger = logging.getLogger(__name__)

_client = None


def client():  # pragma: no cover - thin wrapper over the GCP client
    global _client
    if _client is None:
        from google.cloud import firestore

        _client = firestore.Client(
            project=config.PROJECT_ID,
            database=config.FIRESTORE_DATABASE,
        )
    return _client


def _config_collection():
    return client().collection(config.CONFIG_COLLECTION)


def _settings_ref():
    return _config_collection().document(config.SETTINGS_DOC)


def _state_ref():
    return _config_collection().document(config.STATE_DOC)


def load_settings() -> Settings:
    """Current settings, seeding defaults on first run.

    A malformed document falls back to defaults rather than taking the
    service down — but it fails towards *not trading*, which is the safe
    direction.
    """
    try:
        snapshot = _settings_ref().get()
    except Exception as exc:  # noqa: BLE001 - infrastructure failure
        log(logger, logging.ERROR, "settings read failed", error=str(exc))
        return default_settings()

    if not snapshot.exists:
        settings = default_settings()
        save_settings_document(settings.model_dump(), actor="system:bootstrap")
        return settings

    raw = snapshot.to_dict() or {}
    try:
        return Settings(**raw)
    except Exception as exc:  # noqa: BLE001 - validation failure
        log(
            logger,
            logging.ERROR,
            "settings document invalid, falling back to defaults",
            error=str(exc),
        )
        fallback = default_settings()
        fallback.execution.trading_enabled = False
        fallback.risk.kill_switch = True
        fallback.risk.kill_switch_reason = "settings document failed validation"
        return fallback


def save_settings_document(document: dict[str, Any], actor: str) -> None:
    document = dict(document)
    document["updated_at"] = datetime.now(timezone.utc).isoformat()
    document["updated_by"] = actor
    _settings_ref().set(document)


def update_settings(
    patch: dict[str, Any], actor: str = "dashboard"
) -> tuple[Settings, list[dict[str, Any]]]:
    """Apply a partial update, validate it, persist it, audit it.

    Validation happens against the *merged* document, so a patch cannot
    sneak past a constraint by omitting the field it depends on.
    """
    current = load_settings()
    before = current.model_dump()

    merged = merge_settings(before, patch)
    settings = Settings(**merged)  # raises on invalid input, before any write

    after = settings.model_dump()
    changes = diff_settings(before, after)

    save_settings_document(after, actor)
    if changes:
        write_audit(changes, actor)
        log(
            logger,
            logging.INFO,
            "settings updated",
            actor=actor,
            fields=[c["field"] for c in changes],
        )
    return settings, changes


def write_audit(changes: list[dict[str, Any]], actor: str, note: str = "") -> None:
    """Record a settings change. Values are redacted on the way in."""
    from logging_setup import redact_value

    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "note": note,
        "changes": redact_value(changes),
    }
    try:
        _settings_ref().collection(config.AUDIT_SUBCOLLECTION).add(entry)
    except Exception as exc:  # noqa: BLE001 - audit must not break the write
        log(logger, logging.ERROR, "audit write failed", error=str(exc))


def read_audit(limit: int = 100) -> list[dict[str, Any]]:
    from google.cloud import firestore

    try:
        query = (
            _settings_ref()
            .collection(config.AUDIT_SUBCOLLECTION)
            .order_by("at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [doc.to_dict() | {"id": doc.id} for doc in query.stream()]
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "audit read failed", error=str(exc))
        return []


# --- kill switch ----------------------------------------------------------


def set_kill_switch(engaged: bool, reason: str, actor: str = "dashboard") -> Settings:
    """Engage or release the kill switch.

    Written straight to Firestore and read fresh by every execution decision,
    so it takes effect on the next trade rather than the next deploy.
    Scanning is untouched: the point is to stop trading while continuing to
    see what is happening.
    """
    settings, _ = update_settings(
        {"risk": {"kill_switch": engaged, "kill_switch_reason": reason}},
        actor=actor,
    )
    write_audit(
        [{"field": "risk.kill_switch", "from": not engaged, "to": engaged}],
        actor,
        note=reason or ("kill switch engaged" if engaged else "kill switch released"),
    )
    log(
        logger,
        logging.WARNING if engaged else logging.INFO,
        "kill switch engaged" if engaged else "kill switch released",
        actor=actor,
        reason=reason,
    )
    return settings


def is_killed() -> bool:
    """Fresh read. Never cached — that is the whole point."""
    try:
        snapshot = _settings_ref().get()
        if not snapshot.exists:
            return False
        return bool((snapshot.to_dict() or {}).get("risk", {}).get("kill_switch"))
    except Exception as exc:  # noqa: BLE001
        # If we cannot confirm the switch is released, treat it as engaged.
        log(logger, logging.ERROR, "kill switch read failed, assuming engaged", error=str(exc))
        return True


# --- runtime state --------------------------------------------------------


def _today() -> str:
    return date.today().isoformat()


def load_state() -> dict[str, Any]:
    try:
        snapshot = _state_ref().get()
        state = snapshot.to_dict() if snapshot.exists else {}
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "state read failed", error=str(exc))
        state = {}

    state = state or {}
    # Daily counters reset on date change.
    if state.get("day") != _today():
        state = {
            "day": _today(),
            "trades_today": 0,
            "realised_pnl_today": 0.0,
            "unmatched_legs_today": 0,
            "failed_unwinds_today": 0,
            "errors_today": 0,
            "last_scan_at": state.get("last_scan_at"),
            "last_scan_id": state.get("last_scan_id"),
            "unmatched_legs_total": state.get("unmatched_legs_total", 0),
            "failed_unwinds_total": state.get("failed_unwinds_total", 0),
        }
    return state


def save_state(state: dict[str, Any]) -> None:
    try:
        _state_ref().set(state)
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.ERROR, "state write failed", error=str(exc))


def bump_state(**deltas: float) -> dict[str, Any]:
    """Increment daily counters atomically enough for our cadence.

    Scans are minutes apart and single-flighted by Cloud Scheduler, so a
    read-modify-write is sufficient; a transaction here would buy nothing.
    """
    state = load_state()
    for key, delta in deltas.items():
        state[key] = state.get(key, 0) + delta
    save_state(state)
    return state


def record_scan(scan_id: str, at: Optional[datetime] = None) -> None:
    state = load_state()
    state["last_scan_id"] = scan_id
    state["last_scan_at"] = (at or datetime.now(timezone.utc)).isoformat()
    save_state(state)
