"""Integration tests for the OAuth FastAPI routes.

OAuthService is replaced by a lightweight fake so no network or OpenBao is needed.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from epicurus_core_app.oauth.models import (
    PROVIDER_GOOGLE,
    OAuthClientStatus,
    OAuthConnectResponse,
    OAuthStatus,
    OAuthTokenResponse,
)
from epicurus_core_app.oauth.routes import create_oauth_router
from epicurus_core_app.oauth.service import OAuthError

DEFAULT_TENANT = "local"


class _FakeOAuthService:
    """Stand-in for OAuthService — records calls and returns seeded results."""

    def __init__(
        self,
        connect_result: OAuthConnectResponse | None = None,
        status_result: OAuthStatus | None = None,
        token_result: OAuthTokenResponse | None = None,
        callback_result: tuple[str, str] | None = None,
        client_status_result: OAuthClientStatus | None = None,
        raise_on: str | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._connect = connect_result or OAuthConnectResponse(
            auth_url="https://accounts.google.com/auth"
        )
        self._status = status_result or OAuthStatus(provider=PROVIDER_GOOGLE, connected=False)
        self._token = token_result or OAuthTokenResponse(
            access_token="ya29.test", token_type="Bearer"
        )
        self._callback = callback_result or (PROVIDER_GOOGLE, DEFAULT_TENANT)
        self._client_status = client_status_result or OAuthClientStatus(
            provider=PROVIDER_GOOGLE, configured=False
        )
        self._raise_on = raise_on
        self._raise_exc = raise_exc or OAuthError("test error")
        self.calls: list[tuple[str, ...]] = []

    def _maybe_raise(self, method: str) -> None:
        if self._raise_on == method:
            raise self._raise_exc

    async def set_client_credentials(
        self, provider: str, client_id: str, client_secret: str, tenant_id: str
    ) -> None:
        self.calls.append(("set_client", provider, client_id, tenant_id))
        self._maybe_raise("set_client")

    async def get_client_status(self, provider: str, tenant_id: str) -> OAuthClientStatus:
        self.calls.append(("get_client", provider, tenant_id))
        self._maybe_raise("get_client")
        return self._client_status

    async def connect(self, provider: str, tenant_id: str, **_: object) -> OAuthConnectResponse:
        self.calls.append(("connect", provider, tenant_id))
        self._maybe_raise("connect")
        return self._connect

    async def handle_callback(self, code: str, state: str) -> tuple[str, str]:
        self.calls.append(("callback", code, state))
        self._maybe_raise("callback")
        return self._callback

    async def get_status(self, provider: str, tenant_id: str) -> OAuthStatus:
        self.calls.append(("status", provider, tenant_id))
        self._maybe_raise("status")
        return self._status

    async def disconnect(self, provider: str, tenant_id: str) -> None:
        self.calls.append(("disconnect", provider, tenant_id))
        self._maybe_raise("disconnect")

    async def get_token(self, provider: str, tenant_id: str) -> OAuthTokenResponse:
        self.calls.append(("token", provider, tenant_id))
        self._maybe_raise("token")
        return self._token


def _app(fake: _FakeOAuthService) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_oauth_router(fake, default_tenant=DEFAULT_TENANT)  # type: ignore[arg-type]
    )
    return app


# ── GET /{provider}/connect ───────────────────────────────────────────────────


async def test_connect_returns_auth_url() -> None:
    fake = _FakeOAuthService(
        connect_result=OAuthConnectResponse(auth_url="https://accounts.google.com/auth?foo=bar")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/connect")
    assert resp.status_code == 200
    assert "accounts.google.com" in resp.json()["auth_url"]


async def test_connect_passes_tenant_id_from_query() -> None:
    fake = _FakeOAuthService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/connect?tenant_id=workspace-1")
    assert fake.calls[0] == ("connect", PROVIDER_GOOGLE, "workspace-1")


async def test_connect_defaults_to_default_tenant() -> None:
    fake = _FakeOAuthService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/connect")
    assert fake.calls[0][2] == DEFAULT_TENANT


async def test_connect_oauth_error_returns_400() -> None:
    fake = _FakeOAuthService(raise_on="connect")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/connect")
    assert resp.status_code == 400


# ── GET /callback ─────────────────────────────────────────────────────────────


async def test_callback_success_redirects_to_settings() -> None:
    fake = _FakeOAuthService(callback_result=(PROVIDER_GOOGLE, DEFAULT_TENANT))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/platform/v1/oauth/callback?code=mycode&state=mystate")
    assert resp.status_code == 302
    assert f"oauth_connected={PROVIDER_GOOGLE}" in resp.headers["location"]


async def test_callback_error_param_redirects_to_settings_error() -> None:
    fake = _FakeOAuthService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/platform/v1/oauth/callback?code=x&state=y&error=access_denied")
    assert resp.status_code == 302
    assert "oauth_error=1" in resp.headers["location"]


async def test_callback_service_error_redirects_to_error() -> None:
    fake = _FakeOAuthService(raise_on="callback")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        resp = await client.get("/platform/v1/oauth/callback?code=x&state=y")
    assert resp.status_code == 302
    assert "oauth_error=1" in resp.headers["location"]


# ── GET /{provider}/status ────────────────────────────────────────────────────


async def test_status_connected() -> None:
    fake = _FakeOAuthService(
        status_result=OAuthStatus(provider=PROVIDER_GOOGLE, connected=True, scope="openid email")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is True
    assert resp.json()["scope"] == "openid email"


async def test_status_not_connected() -> None:
    fake = _FakeOAuthService(status_result=OAuthStatus(provider=PROVIDER_GOOGLE, connected=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


async def test_status_oauth_error_returns_400() -> None:
    fake = _FakeOAuthService(raise_on="status")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/status")
    assert resp.status_code == 400


# ── DELETE /{provider} ────────────────────────────────────────────────────────


async def test_disconnect_returns_ok() -> None:
    fake = _FakeOAuthService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.delete(f"/platform/v1/oauth/{PROVIDER_GOOGLE}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert fake.calls[0][0] == "disconnect"


async def test_disconnect_error_returns_400() -> None:
    fake = _FakeOAuthService(raise_on="disconnect")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.delete(f"/platform/v1/oauth/{PROVIDER_GOOGLE}")
    assert resp.status_code == 400


# ── GET /{provider}/token ─────────────────────────────────────────────────────


async def test_get_token_returns_access_token() -> None:
    fake = _FakeOAuthService(
        token_result=OAuthTokenResponse(
            access_token="ya29.live",
            token_type="Bearer",
            expires_at=9999999999.0,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "ya29.live"
    assert body["token_type"] == "Bearer"


async def test_get_token_not_connected_returns_400() -> None:
    fake = _FakeOAuthService(raise_on="token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/token")
    assert resp.status_code == 400


# ── PUT /{provider}/client ────────────────────────────────────────────────────


async def test_set_client_returns_ok() -> None:
    fake = _FakeOAuthService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/platform/v1/oauth/{PROVIDER_GOOGLE}/client",
            json={"client_id": "my-id", "client_secret": "my-secret"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert fake.calls[0][0] == "set_client"
    assert fake.calls[0][2] == "my-id"


async def test_set_client_secret_not_echoed_in_response() -> None:
    fake = _FakeOAuthService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/platform/v1/oauth/{PROVIDER_GOOGLE}/client",
            json={"client_id": "my-id", "client_secret": "super-secret"},
        )
    body = resp.json()
    assert "super-secret" not in str(body)
    assert "client_secret" not in body


async def test_set_client_error_returns_400() -> None:
    fake = _FakeOAuthService(raise_on="set_client")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/platform/v1/oauth/{PROVIDER_GOOGLE}/client",
            json={"client_id": "x", "client_secret": "y"},
        )
    assert resp.status_code == 400


async def test_set_client_missing_fields_returns_422() -> None:
    fake = _FakeOAuthService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/platform/v1/oauth/{PROVIDER_GOOGLE}/client",
            json={"client_id": "only-id"},
        )
    assert resp.status_code == 422


# ── GET /{provider}/client ────────────────────────────────────────────────────


async def test_get_client_status_configured() -> None:
    fake = _FakeOAuthService(
        client_status_result=OAuthClientStatus(provider=PROVIDER_GOOGLE, configured=True)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/client")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["provider"] == PROVIDER_GOOGLE
    assert "client_secret" not in body


async def test_get_client_status_not_configured() -> None:
    fake = _FakeOAuthService(
        client_status_result=OAuthClientStatus(provider=PROVIDER_GOOGLE, configured=False)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/client")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


async def test_get_client_status_error_returns_400() -> None:
    fake = _FakeOAuthService(raise_on="get_client")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(fake)), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/oauth/{PROVIDER_GOOGLE}/client")
    assert resp.status_code == 400
