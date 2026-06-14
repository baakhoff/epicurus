"""OAuth 2.0 service — connect flow, token exchange, transparent refresh, and vault storage.

The operator provisions client credentials (client_id + client_secret) into OpenBao
once (via Settings UI or CLI). The service uses them to run the authorization-code
flow per tenant, stores the resulting tokens back in OpenBao, and refreshes them
transparently when they expire.  Modules never see a client secret or refresh token —
they call ``GET /platform/v1/oauth/{provider}/token`` and receive a valid access token.

Secret paths (all tenant-scoped via ``scope_secret_path``):
    ``oauth/clients/{provider}``  → operator-provisioned: ``{client_id, client_secret}``
    ``oauth/tokens/{provider}``   → user-granted: ``{access_token, refresh_token,
                                    expires_at, scope, token_type}``
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from epicurus_core import SecretError, SecretStore

from .models import (
    PROVIDER_GOOGLE,
    SUPPORTED_PROVIDERS,
    OAuthClientStatus,
    OAuthConnectResponse,
    OAuthStatus,
    OAuthTokenResponse,
)

# Google OAuth 2.0 endpoints
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Default scopes requested when connecting to Google.
# openid + email give the agent a verified identity; additional scopes
# (calendar, gmail, drive, …) are declared per-module and requested during their
# own connect flows (future).
_GOOGLE_DEFAULT_SCOPE = "openid email profile"

# Tokens will be proactively refreshed this many seconds before they actually expire.
_REFRESH_BUFFER_SECONDS = 120

# The placeholder shipped as the config default. The signed state token is the entire
# CSRF / token-injection defense, so the flow must refuse to run while the secret is
# still this value (or empty) — otherwise anyone could forge a valid state.
_PLACEHOLDER_STATE_SECRET = b"change-this-before-use"


class OAuthError(RuntimeError):
    """Raised when an OAuth operation fails (invalid state, provider error, etc.)."""


class OAuthService:
    """Per-tenant OAuth 2.0 connect flow and token vault.

    Args:
        secrets: The OpenBao client shared with the rest of the core.
        redirect_base_url: The public base URL of the server, used to build
            ``redirect_uri`` for the provider callback.
        state_secret: An HMAC secret for signing the ``state`` parameter
            (CSRF protection). Must be kept stable across restarts; rotate it
            to invalidate any in-flight connect flows.
    """

    def __init__(
        self,
        secrets: SecretStore,
        *,
        redirect_base_url: str,
        state_secret: str,
    ) -> None:
        self._secrets = secrets
        self._redirect_base = redirect_base_url.rstrip("/")
        self._state_secret = state_secret.encode()

    # ── secret-path helpers ──────────────────────────────────────────────────

    @staticmethod
    def _client_path(provider: str) -> str:
        return f"oauth/clients/{provider}"

    @staticmethod
    def _token_path(provider: str) -> str:
        return f"oauth/tokens/{provider}"

    def _redirect_uri(self) -> str:
        return f"{self._redirect_base}/platform/v1/oauth/callback"

    # ── state token (CSRF) ───────────────────────────────────────────────────

    def _require_configured_secret(self) -> None:
        """Refuse to run the flow with an unset or placeholder state secret — the
        signed state is the only defense against a forged-state token injection."""
        if not self._state_secret or self._state_secret == _PLACEHOLDER_STATE_SECRET:
            raise OAuthError(
                "OAUTH_STATE_SECRET is unset or still the placeholder default — set it to "
                "a strong random value (e.g. `openssl rand -hex 32`) before using OAuth"
            )

    def _create_state(self, provider: str, tenant_id: str) -> str:
        """Build a signed, time-limited state token."""
        payload = json.dumps(
            {
                "p": provider,
                "t": tenant_id,
                "n": secrets.token_hex(8),
                "exp": int(time.time()) + 600,  # 10-minute window
            },
            separators=(",", ":"),
        )
        sig = hmac.new(self._state_secret, payload.encode(), hashlib.sha256).hexdigest()
        raw = f"{payload}.{sig}"
        return base64.urlsafe_b64encode(raw.encode()).decode()

    def _verify_state(self, state: str) -> tuple[str, str]:
        """Verify a state token and return ``(provider, tenant_id)``.

        Raises :class:`OAuthError` if the signature is invalid or the token expired.
        """
        try:
            raw = base64.urlsafe_b64decode(state.encode()).decode()
            payload_str, _, sig = raw.rpartition(".")
            expected = hmac.new(
                self._state_secret, payload_str.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise OAuthError("invalid state signature")
            parsed: dict[str, Any] = json.loads(payload_str)
            if time.time() > parsed["exp"]:
                raise OAuthError("state token expired")
            return parsed["p"], parsed["t"]
        except OAuthError:
            raise
        except Exception as exc:
            raise OAuthError(f"malformed state token: {exc}") from exc

    # ── provider dispatch ────────────────────────────────────────────────────

    def _auth_url(self, provider: str, client_id: str, scope: str, state: str) -> str:
        if provider == PROVIDER_GOOGLE:
            params = {
                "client_id": client_id,
                "redirect_uri": self._redirect_uri(),
                "response_type": "code",
                "scope": scope,
                "state": state,
                "access_type": "offline",
                "prompt": "consent",  # always return a refresh token
                # accumulate previously-granted scopes so connecting a second Google
                # module doesn't clobber the first module's grant (#102)
                "include_granted_scopes": "true",
            }
            return _GOOGLE_AUTH_URL + "?" + urlencode(params)
        raise OAuthError(f"unsupported provider: {provider!r}")

    async def _exchange_code(
        self, provider: str, code: str, client_id: str, client_secret: str
    ) -> dict[str, Any]:
        if provider == PROVIDER_GOOGLE:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uri": self._redirect_uri(),
                        "grant_type": "authorization_code",
                    },
                )
                if resp.status_code != 200:
                    raise OAuthError(f"token exchange failed ({resp.status_code}): {resp.text}")
                return dict(resp.json())
        raise OAuthError(f"unsupported provider: {provider!r}")

    async def _refresh_access_token(
        self, provider: str, refresh_token: str, client_id: str, client_secret: str
    ) -> dict[str, Any]:
        if provider == PROVIDER_GOOGLE:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "refresh_token",
                    },
                )
                if resp.status_code != 200:
                    raise OAuthError(f"token refresh failed ({resp.status_code}): {resp.text}")
                return dict(resp.json())
        raise OAuthError(f"unsupported provider: {provider!r}")

    @staticmethod
    def _union_scopes(existing: str, new: str) -> str:
        """Return the union of two space-separated scope strings, preserving order."""
        seen: set[str] = set()
        merged: list[str] = []
        for part in (existing + " " + new).split():
            if part not in seen:
                seen.add(part)
                merged.append(part)
        return " ".join(merged)

    # ── public API ───────────────────────────────────────────────────────────

    def _validate_provider(self, provider: str) -> None:
        if provider not in SUPPORTED_PROVIDERS:
            raise OAuthError(
                f"unknown provider {provider!r}; supported: {sorted(SUPPORTED_PROVIDERS)}"
            )

    async def set_client_credentials(
        self, provider: str, client_id: str, client_secret: str, tenant_id: str
    ) -> None:
        """Store operator-supplied client credentials in the vault.

        Write-only: the secret is never read back through this path.
        Callers should use :meth:`get_client_status` to check whether credentials
        exist without exposing the secret.
        """
        self._validate_provider(provider)
        await self._secrets.set(
            self._client_path(provider),
            {"client_id": client_id, "client_secret": client_secret},
            tenant_id,
        )

    async def get_client_status(self, provider: str, tenant_id: str) -> OAuthClientStatus:
        """Return whether client credentials are configured — never returns the secret."""
        self._validate_provider(provider)
        try:
            await self._secrets.get(self._client_path(provider), tenant_id)
            return OAuthClientStatus(provider=provider, configured=True)
        except SecretError:
            return OAuthClientStatus(provider=provider, configured=False)

    async def connect(
        self, provider: str, tenant_id: str, *, scope: str | None = None
    ) -> OAuthConnectResponse:
        """Build the provider consent URL and return it to the caller.

        The caller (web shell) redirects the browser to ``auth_url``; the
        provider will call back to ``/platform/v1/oauth/callback``.
        """
        self._validate_provider(provider)
        self._require_configured_secret()
        try:
            creds = await self._secrets.get(self._client_path(provider), tenant_id)
        except SecretError as exc:
            raise OAuthError(
                f"no OAuth client credentials for provider {provider!r} — "
                "add them in Settings or store them in OpenBao at "
                f"'oauth/clients/{provider}' (client_id, client_secret)"
            ) from exc
        client_id: str = creds["client_id"]
        effective_scope = scope or _GOOGLE_DEFAULT_SCOPE
        state = self._create_state(provider, tenant_id)
        auth_url = self._auth_url(provider, client_id, effective_scope, state)
        return OAuthConnectResponse(auth_url=auth_url)

    async def handle_callback(self, code: str, state: str) -> tuple[str, str]:
        """Process the provider callback: exchange code, store tokens.

        Returns ``(provider, tenant_id)`` so the caller can redirect accordingly.
        The stored scope is the *union* of any previously-granted scopes and the
        scopes returned by this grant, so connecting a second Google module does
        not clobber the first module's grant (#102).
        """
        self._require_configured_secret()
        provider, tenant_id = self._verify_state(state)
        self._validate_provider(provider)
        try:
            creds = await self._secrets.get(self._client_path(provider), tenant_id)
        except SecretError as exc:
            raise OAuthError(f"client credentials missing for {provider!r}") from exc

        token_data = await self._exchange_code(
            provider, code, creds["client_id"], creds["client_secret"]
        )

        # Compute absolute expiry timestamp from the ``expires_in`` seconds field.
        expires_at: float | None = None
        if "expires_in" in token_data:
            expires_at = time.time() + float(token_data["expires_in"])

        # Accumulate scopes: union with whatever was previously stored so that
        # connecting a second Google module doesn't clobber the first one's grant.
        new_scope = token_data.get("scope", "")
        try:
            prev = await self._secrets.get(self._token_path(provider), tenant_id)
            new_scope = self._union_scopes(prev.get("scope", ""), new_scope)
        except SecretError:
            pass  # first connect — no prior token to merge

        await self._secrets.set(
            self._token_path(provider),
            {
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", ""),
                "token_type": token_data.get("token_type", "Bearer"),
                "scope": new_scope,
                "expires_at": expires_at,
            },
            tenant_id,
        )
        return provider, tenant_id

    async def get_token(self, provider: str, tenant_id: str) -> OAuthTokenResponse:
        """Return a valid access token, refreshing transparently if needed.

        Raises :class:`OAuthError` if the provider is not connected.
        """
        self._validate_provider(provider)
        try:
            stored = await self._secrets.get(self._token_path(provider), tenant_id)
        except SecretError as exc:
            raise OAuthError(
                f"provider {provider!r} is not connected for tenant {tenant_id!r}"
            ) from exc

        expires_at: float | None = stored.get("expires_at")
        needs_refresh = (
            expires_at is not None and time.time() >= expires_at - _REFRESH_BUFFER_SECONDS
        )

        if needs_refresh:
            refresh_token: str = stored.get("refresh_token", "")
            if not refresh_token:
                raise OAuthError(
                    f"access token expired for {provider!r} and no refresh token is stored — "
                    "the user must reconnect"
                )
            try:
                creds = await self._secrets.get(self._client_path(provider), tenant_id)
            except SecretError as exc:
                raise OAuthError(f"client credentials missing for {provider!r}") from exc

            new_data = await self._refresh_access_token(
                provider, refresh_token, creds["client_id"], creds["client_secret"]
            )
            new_expires_at: float | None = None
            if "expires_in" in new_data:
                new_expires_at = time.time() + float(new_data["expires_in"])

            stored = {
                "access_token": new_data["access_token"],
                "refresh_token": new_data.get("refresh_token") or refresh_token,
                "token_type": new_data.get("token_type", "Bearer"),
                "scope": new_data.get("scope", stored.get("scope", "")),
                "expires_at": new_expires_at,
            }
            await self._secrets.set(self._token_path(provider), stored, tenant_id)
            expires_at = new_expires_at

        return OAuthTokenResponse(
            access_token=stored["access_token"],
            token_type=stored.get("token_type", "Bearer"),
            expires_at=expires_at,
        )

    async def get_status(self, provider: str, tenant_id: str) -> OAuthStatus:
        """Check whether the provider is connected (tokens are stored)."""
        self._validate_provider(provider)
        try:
            stored = await self._secrets.get(self._token_path(provider), tenant_id)
            return OAuthStatus(
                provider=provider,
                connected=True,
                scope=stored.get("scope") or None,
            )
        except SecretError:
            return OAuthStatus(provider=provider, connected=False)

    async def disconnect(self, provider: str, tenant_id: str) -> None:
        """Remove stored tokens, effectively disconnecting the provider."""
        self._validate_provider(provider)
        with contextlib.suppress(SecretError):
            await self._secrets.delete(self._token_path(provider), tenant_id)
