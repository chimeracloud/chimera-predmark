"""Credential storage in Google Secret Manager.

The rule this module exists to enforce: a credential goes in and never comes
back out to a caller. The settings page can write one and can ask whether one
is configured. There is no code path — no endpoint, no debug flag, no admin
override — that returns a stored value to a client.

A wallet private key is not a password. It is the funds themselves, and a
leak is irreversible with no recourse. So:

  * values are written to Secret Manager, never to Firestore or config
  * reads happen only inside the execution path, into a local variable that
    dies with the request
  * `describe` returns a masked confirmation and the last four characters,
    which is enough for Charles to tell one key from another and useless to
    anyone else
  * nothing here logs a value, and `logging_setup` redacts as a second line
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import config
from logging_setup import log
from venues.registry import VENUES, credential_fields, venue_spec

logger = logging.getLogger(__name__)

# Cache of secret versions within a single container lifetime. Cloud Run
# recycles the container regularly, and the kill switch and settings live in
# Firestore rather than here, so a bounded cache cannot pin a stale
# credential the way the lay engine's in-process loop did.
_CACHE: dict[str, str] = {}


class SecretsUnavailable(RuntimeError):
    """Secret Manager could not be reached or the secret does not exist."""


def _client():  # pragma: no cover - thin wrapper over the GCP client
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient()


def _parent() -> str:
    return f"projects/{config.PROJECT_ID}"


def _secret_path(secret_id: str) -> str:
    return f"{_parent()}/secrets/{secret_id}"


def mask(value: str) -> str:
    """A confirmation, not a value.

    Shows only the last four characters, and only when the value is long
    enough that four characters cannot meaningfully narrow it down.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"****{value[-4:]}"


def write_secret(venue: str, key: str, value: str) -> dict[str, Any]:
    """Store a credential, creating the secret on first write.

    Adds a new version rather than replacing: rotation stays auditable, and
    a mistyped key can be rolled back by disabling a version.
    """
    from google.api_core import exceptions as gcp_exceptions

    spec = venue_spec(venue)
    secret_id = next(
        (c.secret_id for c in spec.credentials if c.key == key),
        None,
    )
    if not secret_id:
        raise ValueError(f"unknown credential '{key}' for venue '{venue}'")

    value = value.strip()
    if not value:
        raise ValueError("credential value is empty")

    client = _client()

    # Add a version to the existing secret. The seven predmark secrets are
    # provisioned up front so the runtime account can hold
    # secretVersionAdder on those six alone rather than project-wide
    # secretmanager.admin — which would also hand it every other service's
    # credentials in this project. Creating a secret is therefore expected
    # to be forbidden, and is only attempted as a fallback for a deployment
    # whose secrets have not been pre-provisioned.
    def _add_version():
        return client.add_secret_version(
            request={
                "parent": _secret_path(secret_id),
                "payload": {"data": value.encode("utf-8")},
            }
        )

    try:
        version = _add_version()
    except gcp_exceptions.NotFound:
        try:
            client.create_secret(
                request={
                    "parent": _parent(),
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        except gcp_exceptions.AlreadyExists:
            pass
        except gcp_exceptions.PermissionDenied as exc:
            raise SecretsUnavailable(
                f"secret {secret_id} does not exist and this service account "
                "cannot create it — provision it first"
            ) from exc
        version = _add_version()
    except gcp_exceptions.PermissionDenied as exc:
        raise SecretsUnavailable(
            f"the service account cannot add a version to {secret_id}"
        ) from exc
    _CACHE.pop(secret_id, None)

    # Note what was written, never what it was.
    log(
        logger,
        logging.INFO,
        "credential stored",
        venue=venue,
        credential=key,
        secret_id=secret_id,
        version=version.name.rsplit("/", 1)[-1],
    )
    return {
        "venue": venue,
        "key": key,
        "secret_id": secret_id,
        "configured": True,
        "masked": mask(value),
        "version": version.name.rsplit("/", 1)[-1],
    }


def read_secret(secret_id: str, use_cache: bool = True) -> Optional[str]:
    """Fetch the latest version. Internal use only — never returned to a client."""
    if use_cache and secret_id in _CACHE:
        return _CACHE[secret_id]

    from google.api_core import exceptions as gcp_exceptions
    from google.auth import exceptions as auth_exceptions

    try:
        response = _client().access_secret_version(
            request={"name": f"{_secret_path(secret_id)}/versions/latest"}
        )
    except gcp_exceptions.NotFound:
        return None
    except gcp_exceptions.FailedPrecondition:
        # The latest version is DESTROYED or DISABLED. That is the same
        # situation as "not configured" — there is no usable credential — and
        # it must read as such rather than propagating. Uncaught, this
        # returned a 500 from /scan and took detection down entirely for a
        # venue whose key had simply been rotated away.
        log(
            logger,
            logging.WARNING,
            "secret has no usable version — treating as not configured",
            secret_id=secret_id,
        )
        return None
    except gcp_exceptions.PermissionDenied as exc:
        raise SecretsUnavailable(
            f"the service account cannot read secret {secret_id}"
        ) from exc
    except (auth_exceptions.GoogleAuthError, OSError) as exc:
        # No credentials or no route to Secret Manager. Returning None rather
        # than raising lets a scan continue on public market data — reads
        # need no venue credentials — while the pre-trade checks still block
        # every trade, because an uncredentialed venue never enters
        # `credentialed_venues`. Detection degrades; execution stops.
        log(
            logger,
            logging.ERROR,
            "secret manager unreachable — continuing without credentials",
            secret_id=secret_id,
            error=type(exc).__name__,
        )
        return None

    # Strip whitespace. A credential entered through the Secret Manager
    # console, or piped in with `echo` rather than `printf`, arrives with a
    # trailing newline. For an API key that means an auth failure the venue
    # reports only as "invalid signature"; for a header value it is outright
    # illegal. PEM blocks are unaffected — their internal newlines are kept,
    # only the surrounding whitespace goes.
    value = response.payload.data.decode("utf-8").strip()
    _CACHE[secret_id] = value
    return value


def load_credentials(venue: str) -> dict[str, str]:
    """Assemble the credentials object pmxt expects for one venue.

    Missing optional credentials are simply absent. Missing required ones
    produce an empty result, because a half-credentialed trading client
    fails at the worst possible moment — mid-execution, with one leg already
    filled.
    """
    credentials: dict[str, str] = {}
    missing_required: list[str] = []

    for field in credential_fields(venue):
        value = read_secret(field.secret_id)
        if value:
            credentials[field.key] = value
        elif field.required_for_trading:
            missing_required.append(field.key)

    if missing_required:
        log(
            logger,
            logging.WARNING,
            "venue not fully credentialed",
            venue=venue,
            missing=missing_required,
        )
        return {}

    # Polymarket's signature type travels with the proxy address; pmxt
    # discovers it when omitted, and gnosis-safe is the default for the proxy
    # wallets the Polymarket UI creates.
    if venue == "polymarket" and credentials.get("funderAddress"):
        credentials.setdefault("signatureType", 2)

    return credentials


def describe_credentials(venue: str) -> list[dict[str, Any]]:
    """Configured-or-not plus a masked tail. Never the value."""
    described: list[dict[str, Any]] = []
    for field in credential_fields(venue):
        try:
            value = read_secret(field.secret_id)
        except SecretsUnavailable:
            described.append(
                {
                    "key": field.key,
                    "label": field.label,
                    "secret_id": field.secret_id,
                    "configured": False,
                    "masked": "",
                    "error": "secret manager unavailable",
                    "required_for_trading": field.required_for_trading,
                    "multiline": field.multiline,
                    "help": field.help,
                }
            )
            continue
        described.append(
            {
                "key": field.key,
                "label": field.label,
                "secret_id": field.secret_id,
                "configured": bool(value),
                "masked": mask(value) if value else "",
                "required_for_trading": field.required_for_trading,
                "multiline": field.multiline,
                "help": field.help,
            }
        )
    return described


def describe_all() -> dict[str, list[dict[str, Any]]]:
    return {venue: describe_credentials(venue) for venue in VENUES}


def credentials_ready(venue: str) -> bool:
    """Whether every required credential for a venue is present."""
    for field in credential_fields(venue):
        if not field.required_for_trading:
            continue
        try:
            if not read_secret(field.secret_id):
                return False
        except SecretsUnavailable:
            return False
    return True


def clear_cache() -> None:
    _CACHE.clear()


# A last check before a value is handed to the settings endpoint: reject
# anything that is obviously a paste of the wrong thing, so a mistyped field
# fails at write time rather than at execution time.
_SHAPES = {
    "privateKey": re.compile(
        r"^(0x[0-9a-fA-F]{64}|[0-9a-fA-F]{64}|-----BEGIN[\s\S]+END[^-]*-----)$"
    ),
    "funderAddress": re.compile(r"^0x[0-9a-fA-F]{40}$"),
}


def validate_shape(venue: str, key: str, value: str) -> Optional[str]:
    """Return an error message if the value is the wrong shape, else None."""
    pattern = _SHAPES.get(key)
    if not pattern:
        return None
    # Limitless and Kalshi both use `privateKey` for different key types; the
    # pattern above accepts hex keys and PEM blocks, which covers all three.
    if not pattern.match(value.strip()):
        return (
            f"value does not look like a valid {key} for {venue} — "
            "check for a truncated paste or a stray newline"
        )
    return None
