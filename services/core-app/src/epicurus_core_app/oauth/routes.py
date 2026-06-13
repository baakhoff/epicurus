"""FastAPI router for the OAuth connect flow and token-vault surface.

User-facing (web shell → core):
    GET  /platform/v1/oauth/{provider}/connect  — initiate the consent flow
    GET  /platform/v1/oauth/callback            — provider callback (redirect after exchange)
    GET  /platform/v1/oauth/{provider}/status   — connected or not
    DELETE /platform/v1/oauth/{provider}        — disconnect (clear stored tokens)

Module-facing (module → core, via the platform API):
    GET  /platform/v1/oauth/{provider}/token    — get a valid (auto-refreshed) access token
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from epicurus_core import get_logger

from .models import OAuthConnectResponse, OAuthStatus, OAuthTokenResponse
from .service import OAuthError, OAuthService

log = get_logger("epicurus_core_app.oauth")


def create_oauth_router(service: OAuthService, *, default_tenant: str) -> APIRouter:
    """Build the ``/platform/v1/oauth`` router."""
    router = APIRouter(prefix="/platform/v1/oauth", tags=["oauth"])

    @router.get("/{provider}/connect", response_model=OAuthConnectResponse)
    async def connect(
        provider: str,
        tenant_id: str = Query(default=default_tenant),
        scope: str | None = Query(default=None),
    ) -> OAuthConnectResponse:
        """Return the provider consent URL.

        The web shell navigates the browser to ``auth_url``; the user grants
        access; the provider calls back to ``GET /platform/v1/oauth/callback``.
        Pass ``scope`` to request non-default OAuth scopes (e.g. Gmail scopes
        in addition to the default ``openid email profile``).
        """
        try:
            return await service.connect(provider, tenant_id, scope=scope)
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/callback")
    async def callback(
        code: str = Query(...),
        state: str = Query(...),
        error: str | None = Query(default=None),
        error_description: str | None = Query(default=None),
    ) -> RedirectResponse:
        """Provider OAuth callback — exchange code, store tokens, redirect to shell.

        The browser lands here after the user grants (or denies) access.  On
        success the shell is redirected to ``/settings?oauth_connected={provider}``.
        On error it is redirected to ``/settings?oauth_error=1``.
        """
        if error:
            log.warning("oauth callback error", error=error, detail=error_description)
            return RedirectResponse(url="/settings?oauth_error=1", status_code=302)
        try:
            provider, _tenant = await service.handle_callback(code, state)
            return RedirectResponse(url=f"/settings?oauth_connected={provider}", status_code=302)
        except OAuthError as exc:
            log.error("oauth callback failed", error=str(exc))
            return RedirectResponse(url="/settings?oauth_error=1", status_code=302)

    @router.get("/{provider}/status", response_model=OAuthStatus)
    async def status(
        provider: str,
        tenant_id: str = Query(default=default_tenant),
    ) -> OAuthStatus:
        """Whether a provider is currently connected for this tenant."""
        try:
            return await service.get_status(provider, tenant_id)
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/{provider}", response_model=dict)
    async def disconnect(
        provider: str,
        tenant_id: str = Query(default=default_tenant),
    ) -> dict[str, Any]:
        """Disconnect a provider — clears stored tokens from the vault.

        The operator's client credentials are not touched (only user tokens are
        removed). The user can reconnect immediately by starting the connect
        flow again.
        """
        try:
            await service.disconnect(provider, tenant_id)
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok"}

    @router.get("/{provider}/token", response_model=OAuthTokenResponse)
    async def get_token(
        provider: str,
        tenant_id: str = Query(default=default_tenant),
    ) -> OAuthTokenResponse:
        """Return a valid access token, refreshing it transparently if expired.

        Intended for modules: they call this endpoint and receive a ready-to-use
        access token — no client secret or refresh token ever leaves the core.
        """
        try:
            return await service.get_token(provider, tenant_id)
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
