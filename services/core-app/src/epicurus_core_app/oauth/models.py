"""Pydantic models for the OAuth connect flow and token vault."""

from __future__ import annotations

from pydantic import BaseModel

PROVIDER_GOOGLE = "google"
SUPPORTED_PROVIDERS: frozenset[str] = frozenset({PROVIDER_GOOGLE})


class OAuthConnectResponse(BaseModel):
    """Response from the initiate-connect endpoint: the URL to redirect the user to."""

    auth_url: str


class OAuthStatus(BaseModel):
    """Whether a provider is currently connected for a given tenant."""

    provider: str
    connected: bool
    scope: str | None = None


class OAuthTokenResponse(BaseModel):
    """A valid access token — returned to modules via the platform API."""

    access_token: str
    token_type: str
    expires_at: float | None = None
