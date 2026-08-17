"""Structured logging for Cloud Run, with credential redaction.

The brief says credentials are never written to logs. Discipline at the call
site is the primary control — nothing in this codebase passes a secret to a
logger. This module is the second line: a filter that rewrites anything
matching the shape of a key before the record reaches stdout. A leaked wallet
key is irreversible, so it is worth paying for the belt as well as the braces.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

# Patterns that look like credential material regardless of context.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # PEM blocks (Kalshi RSA key, Limitless signing key).
    (
        re.compile(
            r"-----BEGIN[^-]{0,40}PRIVATE KEY-----.*?-----END[^-]{0,40}PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[redacted:pem]",
    ),
    # EVM private keys / long hex blobs (Polymarket).
    (re.compile(r"\b0x[0-9a-fA-F]{40,}\b"), "[redacted:hex]"),
    # Bare 64-char hex (private key without the 0x prefix).
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"), "[redacted:hex]"),
    # Anything explicitly labelled as a secret in a mapping-ish string.
    (
        re.compile(
            r"((?:private_?key|api_?key|api_?secret|passphrase|signing_?key|secret)"
            r"\"?\s*[:=]\s*\"?)([^\s,;\"'})\]]{6,})",
            re.IGNORECASE,
        ),
        r"\1[redacted]",
    ),
]

# Field names whose values are dropped wholesale from structured payloads.
_SENSITIVE_KEYS = {
    "privatekey",
    "private_key",
    "apikey",
    "api_key",
    "apisecret",
    "api_secret",
    "apitoken",
    "api_token",
    "passphrase",
    "secret",
    "signingkey",
    "signing_key",
    "credentials",
    "proxyaddress",
    "proxy_address",
    "funderaddress",
    "funder_address",
}


def redact_text(text: str) -> str:
    """Rewrite any credential-shaped substring in ``text``."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a structure destined for a log or an API response."""
    if _depth > 8:
        return "[redacted:depth]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower().replace("-", "_") in _SENSITIVE_KEYS:
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = redact_value(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(item, _depth + 1) for item in value]
    return value


class RedactingJsonFormatter(logging.Formatter):
    """Emit Cloud Logging structured entries with redaction applied."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": redact_text(record.getMessage()),
            "logger": record.name,
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload["context"] = redact_value(extra)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        trace = getattr(record, "trace_id", None)
        if trace:
            payload["logging.googleapis.com/trace"] = trace
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the redacting JSON handler as the only root handler."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingJsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; make them use ours.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False

    # httpx logs full request URLs at INFO. Venue calls never carry secrets in
    # a URL, but the noise is not worth it either.
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")


def log(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    """Log with a structured context payload, redacted."""
    logger.log(level, message, extra={"context": context})
