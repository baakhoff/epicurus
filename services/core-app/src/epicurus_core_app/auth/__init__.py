"""Sign in with an OpenID Connect provider (#969).

The core is an OIDC relying party — authorization code flow with PKCE — and the platform's
trust boundary: with ``AUTH_MODE=oidc`` every request that arrived through a proxy needs a
session, while modules on the internal network are untouched. ``AUTH_MODE=none`` (the default)
is exactly the platform as it was before this package existed.

    config.py      the settings, parsed and validated fail-closed
    oidc.py        discovery, JWKS, the code exchange, ID-token validation, userinfo
    admission.py   who may come in (allowlisted emails, groups, or everyone)
    store.py       the ``auth_sessions`` and ``auth_login_states`` tables
    service.py     sessions (hash, cache, sliding renewal) and the login flow
    middleware.py  the enforcement: proxied → session required; direct → untouched
    routes.py      ``/platform/v1/auth/{session,login,callback,logout}``

:func:`build_auth` is the one call ``create_app`` makes.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncEngine

from epicurus_core import get_logger
from epicurus_core_app.auth.config import AuthConfig, AuthConfigError, load_auth_config
from epicurus_core_app.auth.errors import AUTH_ERROR_CODES, AuthErrorCode, AuthFlowError
from epicurus_core_app.auth.middleware import AuthMiddleware
from epicurus_core_app.auth.oidc import OidcClient
from epicurus_core_app.auth.routes import create_auth_router
from epicurus_core_app.auth.service import AuthIdentity, AuthService, SessionManager
from epicurus_core_app.auth.store import AuthStore
from epicurus_core_app.settings import CoreAppSettings

__all__ = [
    "AUTH_ERROR_CODES",
    "AuthConfig",
    "AuthConfigError",
    "AuthErrorCode",
    "AuthFlowError",
    "AuthIdentity",
    "AuthMiddleware",
    "AuthRuntime",
    "AuthService",
    "build_auth",
]

log = get_logger("epicurus_core_app.auth")


@dataclass(frozen=True)
class AuthRuntime:
    """What ``create_app`` mounts: the service (for the middleware) and its router."""

    service: AuthService
    router: APIRouter

    @property
    def config(self) -> AuthConfig:
        return self.service.config


def build_auth(
    settings: CoreAppSettings,
    engine: AsyncEngine,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AuthRuntime:
    """Validate the sign-in settings and wire the flow. Raises :class:`AuthConfigError`.

    *transport* stands in for the network to the provider (tests pass an
    ``httpx.MockTransport``); ``None`` is the real network with TLS verification on.
    """
    config = load_auth_config(settings)
    store = AuthStore(engine)
    service = AuthService(
        config,
        store=store,
        oidc=OidcClient(config, transport=transport),
        sessions=SessionManager(store, tenant=config.tenant, session_days=config.session_days),
    )
    if config.enabled:
        rules = [
            *(["everyone the provider authenticates"] if config.allow_all_users else []),
            *([f"{len(config.allowed_emails)} email(s)"] if config.allowed_emails else []),
            *([f"{len(config.allowed_groups)} group(s)"] if config.allowed_groups else []),
        ]
        # The line an operator needs to register the client: the callback URL, verbatim.
        log.info(
            "sign-in enabled (OpenID Connect)",
            issuer=config.issuer,
            client_id=config.client_id,
            redirect_uri=config.redirect_uri,
            admits=", ".join(rules),
            public_client=not config.client_secret,
        )
    return AuthRuntime(service=service, router=create_auth_router(service))
