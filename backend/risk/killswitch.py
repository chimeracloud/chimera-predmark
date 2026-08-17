"""The kill switch.

One control that halts execution immediately, without a redeploy and without
a restart. It works because the state lives in Firestore and every execution
decision reads it fresh — there is no cached copy in the process and no
in-memory flag that a new container would start life without.

Scanning is untouched. Stopping trading while continuing to see what the
market is doing is the point; a kill switch that also blinds you is a worse
tool.
"""

from __future__ import annotations

import logging
from typing import Any

from logging_setup import log
from settings import store

logger = logging.getLogger(__name__)


def engage(reason: str, actor: str = "dashboard") -> dict[str, Any]:
    """Halt all execution now."""
    settings = store.set_kill_switch(True, reason, actor)
    log(logger, logging.WARNING, "KILL SWITCH ENGAGED", actor=actor, reason=reason)
    return {
        "kill_switch": True,
        "reason": settings.risk.kill_switch_reason,
        "trading_enabled": settings.execution.trading_enabled,
        "actor": actor,
    }


def release(reason: str, actor: str = "dashboard") -> dict[str, Any]:
    """Permit execution again.

    Releasing the switch does not by itself start trading — `trading_enabled`
    is a separate setting and stays as it was. Two deliberate actions to
    resume, one to stop.
    """
    settings = store.set_kill_switch(False, reason, actor)
    log(logger, logging.WARNING, "kill switch released", actor=actor, reason=reason)
    return {
        "kill_switch": False,
        "reason": reason,
        "trading_enabled": settings.execution.trading_enabled,
        "actor": actor,
    }


def is_engaged() -> bool:
    """Fresh read from Firestore. Deliberately not cached."""
    return store.is_killed()


def status() -> dict[str, Any]:
    settings = store.load_settings()
    return {
        "kill_switch": settings.risk.kill_switch,
        "reason": settings.risk.kill_switch_reason,
        "trading_enabled": settings.execution.trading_enabled,
        "dry_run": settings.execution.dry_run,
    }
