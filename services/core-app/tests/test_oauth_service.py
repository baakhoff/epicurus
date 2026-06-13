"""Unit tests for OAuthService — all HTTP and OpenBao calls are mocked."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from epicurus_core import SecretError
from epicurus_core_app.oauth.models import PROVIDER_GOOGLE
from epicurus_core_app.oauth.service import OAuthError, OAuthService

TEST_SECRET = "test-state-secret"
TEST_TENANT = "local"
TEST_REDIRECT = "http://localhost:8084"

CLIENT_CREDS = {"client_id": "test-client-id", "client_secret": "test-client-secret"}


def _service(
    secrets_get: dict[str, Any] | None = None,
    secrets_raise: Exception | None = None,
) -> tuple[OAuthService, AsyncMock, AsyncMock, AsyncMock]:
    """Build a service with a fake SecretStore."""
    fake_get = AsyncMock()
    if secrets_raise:
        fake_get.side_effect = secrets_raise
    elif secrets_get is not None:
        fake_get.return_value = secrets_get

    fake_set = AsyncMock()
    fake_delete = AsyncMock()

    store = AsyncMock()
    store.get = fake_get
    store.set = fake_set
    store.delete = fake_delete

    svc = OAuthService(
        store,  # type: ignore[arg-type]
        redirect_base_url=TEST_REDIRECT,
        state_secret=TEST_SECRET,
    )
    return svc, fake_get, fake_set, fake_delete


# ── state token round-trip ───────────────────────────────────────────────────


def test_state_roundtrip_returns_provider_and_tenant() -> None:
    svc, *_ = _service()
    state = svc._create_state(PROVIDER_GOOGLE, TEST_TENANT)
    provider, tenant = svc._verify_state(state)
    assert provider == PROVIDER_GOOGLE
    assert tenant == TEST_TENANT


async def test_placeholder_or_empty_state_secret_refuses_to_run() -> None:
    """The flow must refuse an unset/placeholder state secret — it is the CSRF defense."""
    store = AsyncMock()
    for weak in ("change-this-before-use", ""):
        svc = OAuthService(
            store,  # type: ignore[arg-type]
            redirect_base_url=TEST_REDIRECT,
            state_secret=weak,
        )
        with pytest.raises(OAuthError, match="OAUTH_STATE_SECRET"):
            await svc.connect(PROVIDER_GOOGLE, TEST_TENANT)
        with pytest.raises(OAuthError, match="OAUTH_STATE_SECRET"):
            await svc.handle_callback("code", "state")


def test_state_tampered_raises_oauth_error() -> None:
    svc, *_ = _service()
    state = svc._create_state(PROVIDER_GOOGLE, TEST_TENANT)
    # Append a char that makes the HMAC differ — any valid base64 suffix works.
    # The decoded raw string will have a different sig, caught as "invalid state".
    import base64 as _b64

    raw = _b64.urlsafe_b64decode(state.encode() + b"==").decode(errors="replace")
    # Manually corrupt the sig portion (after the last ".")
    payload_part, _, _ = raw.rpartition(".")
    corrupted_raw = payload_part + ".badhmacsig"
    bad_state = _b64.urlsafe_b64encode(corrupted_raw.encode()).decode()
    with pytest.raises(OAuthError, match="invalid state"):
        svc._verify_state(bad_state)


def test_state_wrong_secret_raises_oauth_error() -> None:
    svc_a, *_ = _service()
    svc_b = OAuthService(
        AsyncMock(),  # type: ignore[arg-type]
        redirect_base_url=TEST_REDIRECT,
        state_secret="different-secret",
    )
    state = svc_a._create_state(PROVIDER_GOOGLE, TEST_TENANT)
    with pytest.raises(OAuthError):
        svc_b._verify_state(state)


def test_state_expired_raises_oauth_error() -> None:
    svc, *_ = _service()
    state = svc._create_state(PROVIDER_GOOGLE, TEST_TENANT)
    # Patch time so the token is already expired.
    with (
        patch("epicurus_core_app.oauth.service.time.time", return_value=time.time() + 700),
        pytest.raises(OAuthError, match="expired"),
    ):
        svc._verify_state(state)


# ── connect ──────────────────────────────────────────────────────────────────


async def test_connect_returns_google_auth_url() -> None:
    svc, fake_get, *_ = _service(secrets_get=CLIENT_CREDS)
    result = await svc.connect(PROVIDER_GOOGLE, TEST_TENANT)
    assert "accounts.google.com" in result.auth_url
    assert "test-client-id" in result.auth_url
    fake_get.assert_awaited_once()


async def test_connect_missing_credentials_raises() -> None:
    svc, *_ = _service(secrets_raise=SecretError("not found"))
    with pytest.raises(OAuthError, match="no OAuth client credentials"):
        await svc.connect(PROVIDER_GOOGLE, TEST_TENANT)


async def test_connect_unknown_provider_raises() -> None:
    svc, *_ = _service()
    with pytest.raises(OAuthError, match="unknown provider"):
        await svc.connect("github", TEST_TENANT)


async def test_connect_auth_url_includes_offline_access() -> None:
    svc, *_ = _service(secrets_get=CLIENT_CREDS)
    result = await svc.connect(PROVIDER_GOOGLE, TEST_TENANT)
    assert "offline" in result.auth_url


# ── handle_callback ──────────────────────────────────────────────────────────


async def test_handle_callback_stores_tokens() -> None:
    svc, _fake_get, fake_set, _ = _service(secrets_get=CLIENT_CREDS)
    state = svc._create_state(PROVIDER_GOOGLE, TEST_TENANT)

    token_resp = {
        "access_token": "ya29.access",
        "refresh_token": "1//refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "openid email profile",
    }

    with patch(
        "epicurus_core_app.oauth.service.OAuthService._exchange_code",
        new=AsyncMock(return_value=token_resp),
    ):
        provider, tenant = await svc.handle_callback("code-from-google", state)

    assert provider == PROVIDER_GOOGLE
    assert tenant == TEST_TENANT
    fake_set.assert_awaited_once()
    stored = fake_set.call_args[0][1]
    assert stored["access_token"] == "ya29.access"
    assert stored["refresh_token"] == "1//refresh"
    assert stored["expires_at"] is not None


async def test_handle_callback_invalid_state_raises() -> None:
    svc, *_ = _service()
    with pytest.raises(OAuthError):
        await svc.handle_callback("code", "bad-state")


# ── get_token ────────────────────────────────────────────────────────────────


async def test_get_token_returns_valid_token() -> None:
    stored = {
        "access_token": "ya29.valid",
        "refresh_token": "1//refresh",
        "token_type": "Bearer",
        "scope": "openid",
        "expires_at": time.time() + 1000,  # not expired
    }
    svc, *_ = _service(secrets_get=stored)
    result = await svc.get_token(PROVIDER_GOOGLE, TEST_TENANT)
    assert result.access_token == "ya29.valid"
    assert result.token_type == "Bearer"


async def test_get_token_refreshes_expired_token() -> None:
    stored = {
        "access_token": "ya29.old",
        "refresh_token": "1//refresh",
        "token_type": "Bearer",
        "scope": "openid",
        "expires_at": time.time() - 1,  # already expired
    }
    new_token_data = {
        "access_token": "ya29.new",
        "expires_in": 3600,
        "token_type": "Bearer",
    }

    call_count = 0

    async def _fake_get(path: str, tenant: str) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if "tokens" in path:
            return stored
        return CLIENT_CREDS  # client credentials call

    store = AsyncMock()
    store.get.side_effect = _fake_get
    store.set = AsyncMock()
    svc = OAuthService(
        store,  # type: ignore[arg-type]
        redirect_base_url=TEST_REDIRECT,
        state_secret=TEST_SECRET,
    )

    with patch(
        "epicurus_core_app.oauth.service.OAuthService._refresh_access_token",
        new=AsyncMock(return_value=new_token_data),
    ):
        result = await svc.get_token(PROVIDER_GOOGLE, TEST_TENANT)

    assert result.access_token == "ya29.new"
    store.set.assert_awaited_once()


async def test_get_token_not_connected_raises() -> None:
    svc, *_ = _service(secrets_raise=SecretError("not found"))
    with pytest.raises(OAuthError, match="not connected"):
        await svc.get_token(PROVIDER_GOOGLE, TEST_TENANT)


async def test_get_token_expired_no_refresh_token_raises() -> None:
    stored = {
        "access_token": "ya29.old",
        "refresh_token": "",
        "token_type": "Bearer",
        "scope": "openid",
        "expires_at": time.time() - 1,
    }
    svc, *_ = _service(secrets_get=stored)
    with pytest.raises(OAuthError, match="reconnect"):
        await svc.get_token(PROVIDER_GOOGLE, TEST_TENANT)


# ── get_status ───────────────────────────────────────────────────────────────


async def test_get_status_connected_when_tokens_exist() -> None:
    stored = {
        "access_token": "ya29.access",
        "refresh_token": "1//refresh",
        "token_type": "Bearer",
        "scope": "openid email",
        "expires_at": None,
    }
    svc, *_ = _service(secrets_get=stored)
    status = await svc.get_status(PROVIDER_GOOGLE, TEST_TENANT)
    assert status.connected is True
    assert status.scope == "openid email"


async def test_get_status_not_connected_when_no_tokens() -> None:
    svc, *_ = _service(secrets_raise=SecretError("not found"))
    status = await svc.get_status(PROVIDER_GOOGLE, TEST_TENANT)
    assert status.connected is False


# ── disconnect ───────────────────────────────────────────────────────────────


async def test_disconnect_deletes_token_secret() -> None:
    svc, _, _, fake_delete = _service()
    await svc.disconnect(PROVIDER_GOOGLE, TEST_TENANT)
    fake_delete.assert_awaited_once()


async def test_disconnect_already_disconnected_is_ok() -> None:
    svc, _, _, fake_delete = _service(secrets_raise=SecretError("not found"))
    fake_delete.side_effect = SecretError("not found")
    await svc.disconnect(PROVIDER_GOOGLE, TEST_TENANT)  # should not raise
