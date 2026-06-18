"""FastAPI router for the OAuth connect flow and token-vault surface.

User-facing (web shell → core):
    PUT  /platform/v1/oauth/{provider}/client   — store client credentials (write-only)
    GET  /platform/v1/oauth/{provider}/client   — check whether credentials are configured
    GET  /platform/v1/oauth/{provider}/connect  — initiate the consent flow
    GET  /platform/v1/oauth/callback            — provider callback (redirect after exchange)
    GET  /platform/v1/oauth/{provider}/status   — connected or not
    DELETE /platform/v1/oauth/{provider}        — disconnect (clear stored tokens)

Module-facing (module → core, via the platform API):
    GET  /platform/v1/oauth/{provider}/token    — get a valid (auto-refreshed) access token
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from epicurus_core import get_logger

from .models import (
    OAuthClientCredentials,
    OAuthClientStatus,
    OAuthConnectResponse,
    OAuthStatus,
    OAuthTokenResponse,
)
from .service import OAuthError, OAuthService

log = get_logger("epicurus_core_app.oauth")


class CollectionSync(Protocol):
    """The module-registry hooks the OAuth flow calls on connect / disconnect (#209).

    Satisfied by :class:`~epicurus_core_app.modules.ModuleRegistry`; kept a Protocol so the
    OAuth router doesn't depend on the registry's concrete type and is easy to fake in tests.
    """

    async def autoconnect_collections(self, provider: str) -> list[str]: ...
    async def disconnect_collections(self, provider: str) -> list[str]: ...


def create_oauth_router(
    service: OAuthService,
    *,
    default_tenant: str,
    collections: CollectionSync | None = None,
) -> APIRouter:
    """Build the ``/platform/v1/oauth`` router.

    When *collections* is supplied, connecting a provider auto-seeds the collection
    selection of every module that uses it, and disconnecting clears it (#209) — so the
    operator doesn't hand-wire each module after connecting an account.
    """
    router = APIRouter(prefix="/platform/v1/oauth", tags=["oauth"])

    @router.put("/{provider}/client", response_model=dict)
    async def set_client(
        provider: str,
        body: OAuthClientCredentials,
        tenant_id: str = Query(default=default_tenant),
    ) -> dict[str, Any]:
        """Store the provider's OAuth client credentials in the vault.

        Write-only: the secret is never returned.  Use GET /client to check
        whether credentials are configured.
        """
        try:
            await service.set_client_credentials(
                provider, body.client_id, body.client_secret, tenant_id
            )
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok"}

    @router.get("/{provider}/client", response_model=OAuthClientStatus)
    async def get_client_status(
        provider: str,
        tenant_id: str = Query(default=default_tenant),
    ) -> OAuthClientStatus:
        """Return whether client credentials are configured — never the secret itself."""
        try:
            return await service.get_client_status(provider, tenant_id)
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        except OAuthError as exc:
            log.error("oauth callback failed", error=str(exc))
            return RedirectResponse(url="/settings?oauth_error=1", status_code=302)
        # Auto-connect the modules that use this provider (#209) — best-effort, so a
        # module hiccup never turns a successful grant into an error redirect.
        if collections is not None:
            try:
                seeded = await collections.autoconnect_collections(provider)
                if seeded:
                    log.info("oauth autoconnect seeded modules", provider=provider, modules=seeded)
            except Exception as exc:
                log.warning("oauth autoconnect failed", provider=provider, error=str(exc))
        return RedirectResponse(url=f"/settings?oauth_connected={provider}", status_code=302)

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
        # Clear the now-gone provider from every module's selection (#209) — best-effort.
        if collections is not None:
            try:
                cleared = await collections.disconnect_collections(provider)
                if cleared:
                    log.info("oauth disconnect cleared modules", provider=provider, modules=cleared)
            except Exception as exc:
                log.warning("oauth disconnect cleanup failed", provider=provider, error=str(exc))
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
