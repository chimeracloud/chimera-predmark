"""Settings, audit and credential-handling tests.

The credential tests assert a negative: that no code path returns a stored
value. That is the kind of property which is easy to break with a
well-meaning debug endpoint later, so it is pinned down here.
"""

from __future__ import annotations

import pytest

from logging_setup import RedactingJsonFormatter, redact_text, redact_value
from settings.schema import (
    Settings,
    default_settings,
    diff_settings,
    merge_settings,
)
from settings.secrets import mask, validate_shape


# --- schema ---------------------------------------------------------------


def test_defaults_do_not_trade():
    """A fresh deployment scans but does not trade until told to."""
    settings = default_settings()
    assert settings.execution.trading_enabled is False
    assert settings.execution.dry_run is True
    assert settings.risk.allow_unverified_override is False
    assert settings.risk.required_resolution_status == "MATCHED"
    assert settings.margin.min_margin_to_trade > 0


def test_stake_is_capped_by_the_hard_maximum():
    settings = default_settings()
    settings.execution.stake_per_trade = 5000.0
    settings.execution.max_stake_per_trade = 100.0
    assert settings.effective_stake() == 100.0


def test_unknown_venues_are_rejected():
    with pytest.raises(Exception):
        Settings(venues={"nonexistent-venue": {"pmxt_exchange": "x", "label": "X"}})


def test_enabled_venues_are_ordered_by_poll_priority():
    settings = default_settings()
    for name in settings.venues:
        settings.venues[name].enabled = True
    settings.venues["limitless"].poll_priority = 1
    assert settings.enabled_venues()[0] == "limitless"


# --- merge and diff -------------------------------------------------------


def test_partial_updates_do_not_blank_other_sections():
    """The settings page sends one section; a shallow write would lose the rest."""
    current = {
        "margin": {"min_margin_to_trade": 0.02, "min_margin_to_record": 0.0},
        "risk": {"daily_loss_limit": 50.0},
    }
    merged = merge_settings(current, {"margin": {"min_margin_to_trade": 0.05}})

    assert merged["margin"]["min_margin_to_trade"] == 0.05
    assert merged["margin"]["min_margin_to_record"] == 0.0  # preserved
    assert merged["risk"]["daily_loss_limit"] == 50.0  # preserved


def test_diff_records_field_level_changes():
    """Settings changes are how trading systems break; the record is per field."""
    before = {"execution": {"stake_per_trade": 10.0, "trading_enabled": False}}
    after = {"execution": {"stake_per_trade": 25.0, "trading_enabled": True}}

    changes = diff_settings(before, after)
    fields = {c["field"]: (c["from"], c["to"]) for c in changes}

    assert fields["execution.stake_per_trade"] == (10.0, 25.0)
    assert fields["execution.trading_enabled"] == (False, True)


def test_diff_ignores_bookkeeping_fields():
    changes = diff_settings(
        {"updated_at": "a", "version": 1}, {"updated_at": "b", "version": 2}
    )
    assert changes == []


# --- credentials ----------------------------------------------------------


def test_mask_reveals_only_the_last_four_characters():
    assert mask("0x1234567890abcdef") == "****cdef"
    assert "1234567890" not in mask("0x1234567890abcdef")


def test_mask_reveals_nothing_from_a_short_value():
    """Four characters of an eight-character secret is half of it."""
    assert mask("short123") == "****"
    assert mask("") == ""


def test_credential_shape_validation_catches_a_truncated_paste():
    # A valid EVM key.
    assert validate_shape("polymarket", "privateKey", "0x" + "a" * 64) is None
    # Truncated.
    assert validate_shape("polymarket", "privateKey", "0xabc") is not None
    # A PEM block is also acceptable — Kalshi and Limitless use them.
    pem = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    assert validate_shape("kalshi", "privateKey", pem) is None


def test_address_shape_validation():
    assert validate_shape("polymarket", "funderAddress", "0x" + "b" * 40) is None
    assert validate_shape("polymarket", "funderAddress", "not-an-address") is not None


# --- redaction ------------------------------------------------------------


def test_hex_private_keys_are_redacted_from_log_text():
    key = "0x" + "a" * 64
    assert key not in redact_text(f"submitting with {key}")
    assert "[redacted:hex]" in redact_text(f"submitting with {key}")


def test_bare_hex_keys_are_redacted():
    key = "f" * 64
    assert key not in redact_text(f"key={key}")


def test_pem_blocks_are_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpQIBAAKC\n-----END RSA PRIVATE KEY-----"
    redacted = redact_text(f"kalshi cred {pem}")
    assert "MIIEpQIBAAKC" not in redacted
    assert "[redacted:pem]" in redacted


def test_labelled_secrets_are_redacted():
    assert "supersecretvalue" not in redact_text('api_key="supersecretvalue"')
    assert "supersecretvalue" not in redact_text("passphrase: supersecretvalue")


def test_sensitive_keys_are_dropped_from_structures():
    payload = {
        "venue": "polymarket",
        "credentials": {"privateKey": "0x" + "c" * 64},
        "nested": [{"api_secret": "hunter2"}],
    }
    redacted = redact_value(payload)

    assert redacted["venue"] == "polymarket"  # ordinary fields survive
    assert redacted["credentials"] == "[redacted]"
    assert redacted["nested"][0]["api_secret"] == "[redacted]"
    assert "hunter2" not in str(redacted)


def test_the_log_formatter_redacts_the_message_and_context():
    import logging

    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="placing order with 0x" + "d" * 64, args=(), exc_info=None,
    )
    record.context = {"private_key": "0x" + "e" * 64, "venue": "kalshi"}

    output = RedactingJsonFormatter().format(record)
    assert "d" * 64 not in output
    assert "e" * 64 not in output
    assert "kalshi" in output


def test_settings_endpoint_payload_rejects_credentials():
    """PUT /settings must refuse a credential rather than silently drop it."""
    from main import _contains_credential

    assert _contains_credential({"venues": {"kalshi": {"api_key": "abc"}}}) is True
    assert _contains_credential({"credentials": {}}) is True
    assert _contains_credential({"margin": {"min_margin_to_trade": 0.05}}) is False
