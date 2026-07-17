"""Tests for the shared secret-key redaction rule.

This is the rule ADR-0031 gave the log console, now also guarding the event spine. It is
deliberately blunt (a case-insensitive substring test on key *names*), and these tests pin
that bluntness down on purpose: someone will eventually want to make it clever, and the
over-matching is the point — a false positive hides a debugging field, a false negative
leaks a credential into a browser tab.
"""

from __future__ import annotations

import pytest

from epicurus_core.redaction import is_secret_key, redact_mapping, secret_keys_in


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "key",
        "secret",
        "client_secret",
        "password",
        "credential",
        "credentials",
        "auth",
        "authorization",
        # Case is irrelevant — headers and env vars arrive in every shape.
        "TOKEN",
        "X-Auth-Token",
        "Api_Key",
    ],
)
def test_credential_shaped_keys_match(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key",
    ["message_id", "subject", "count", "tenant", "module", "slug", "path", "unread"],
)
def test_ordinary_keys_do_not_match(key: str) -> None:
    assert not is_secret_key(key)


def test_over_matching_is_deliberate() -> None:
    # "monkey" contains "key". This is a documented false positive, not an oversight:
    # substring matching is what catches "x-auth-token" and "sessionSecret" without a
    # curated list of every naming convention in existence.
    assert is_secret_key("monkey")


def test_redact_mapping_strips_only_secret_keys() -> None:
    data = {"subject": "Re: lunch", "api_key": "sk-123", "unread": 2}
    assert redact_mapping(data) == {"subject": "Re: lunch", "unread": 2}


def test_redact_mapping_leaves_a_clean_mapping_alone() -> None:
    data = {"a": 1, "b": 2}
    assert redact_mapping(data) == data


def test_redact_mapping_returns_a_copy() -> None:
    data = {"api_key": "sk-123"}
    assert redact_mapping(data) == {}
    assert data == {"api_key": "sk-123"}  # the caller's dict is untouched


def test_secret_keys_in_lists_offenders_sorted() -> None:
    # Sorted so an error message naming them is stable and testable.
    assert secret_keys_in({"token": 1, "ok": 2, "api_key": 3}) == ["api_key", "token"]


def test_secret_keys_in_is_empty_for_a_clean_mapping() -> None:
    assert secret_keys_in({"message_id": "1"}) == []
