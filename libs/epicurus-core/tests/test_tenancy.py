"""Tests for the tenant scoping primitive — the dual-track foundation."""

from __future__ import annotations

import pytest

from epicurus_core.tenancy import (
    TenantError,
    current_tenant,
    is_valid_tenant_id,
    reset_current_tenant,
    scope_bucket,
    scope_collection,
    scope_secret_path,
    scope_subject,
    set_current_tenant,
    validate_tenant_id,
)


@pytest.mark.parametrize("tid", ["local", "acme", "tenant-1", "a", "a1b2c3", "a" * 63])
def test_valid_ids(tid: str) -> None:
    assert is_valid_tenant_id(tid)
    assert validate_tenant_id(tid) == tid


@pytest.mark.parametrize(
    "tid",
    ["", "Bad", "tenant_1", "-lead", "trail-", "a" * 64, "white space", "café"],
)
def test_invalid_ids(tid: str) -> None:
    assert not is_valid_tenant_id(tid)
    with pytest.raises(TenantError):
        validate_tenant_id(tid)


def test_scoping_explicit_tenant() -> None:
    assert scope_subject("inbox.message", "acme") == "acme.inbox.message"
    assert scope_collection("notes", "acme") == "acme__notes"
    assert scope_secret_path("google/oauth", "acme") == "tenants/acme/google/oauth"
    assert scope_secret_path("/leading", "acme") == "tenants/acme/leading"
    assert scope_bucket("files", "acme") == "acme-files"


def test_scoping_uses_current_tenant() -> None:
    token = set_current_tenant("acme")
    try:
        assert current_tenant() == "acme"
        assert scope_subject("x") == "acme.x"
        assert scope_bucket("files") == "acme-files"
    finally:
        reset_current_tenant(token)


def test_current_tenant_unset_raises() -> None:
    with pytest.raises(TenantError):
        current_tenant()


def test_scoping_without_tenant_raises() -> None:
    with pytest.raises(TenantError):
        scope_subject("x")


def test_scoping_validates_explicit_tenant() -> None:
    with pytest.raises(TenantError):
        scope_subject("x", "BAD")
