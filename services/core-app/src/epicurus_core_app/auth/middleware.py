"""Where sign-in is enforced: the trust boundary, as pure ASGI middleware (#969).

With ``AUTH_MODE=oidc`` the core asks one question of every request before routing it: **did it
come through a proxy?** A request carrying any of ``X-Forwarded-For``, ``Forwarded``,
``X-Forwarded-Host``, ``X-Forwarded-Proto`` or ``X-Real-IP`` did — the web shell's nginx sets
``X-Forwarded-For`` on everything it passes to ``/platform/``, and so do ingress-nginx and
Traefik (the Compose gateway's ``core-app.localhost`` route). Those requests need a session;
without one they get **401** ``{"detail": "Sign in to continue.", "code": "unauthenticated"}``.
Exempt: ``/health`` and the sign-in endpoints themselves (``/platform/v1/auth/…``).

A request with none of those headers arrived directly on the internal network — a module
calling the platform API (constraint #7), a kubelet probe, Prometheus — and passes untouched,
which is why no module changes. That is also the boundary's limit, and it is deliberate: anyone
who can reach core-app's port *directly* is inside the perimeter this does not guard (the
published loopback port, a cluster-internal client). The web door is what sign-in closes.

Unsafe methods through the proxy also carry a **cross-site check** (403 ``cross_site``):
``Sec-Fetch-Site`` must be ``same-origin`` or ``none``; a browser that sends no
``Sec-Fetch-Site`` must at least send an ``Origin`` equal to the public URL's (or none).
``SameSite=Lax`` alone does not stop a *sibling subdomain* of the same site, and home labs put
many apps under one domain.

Pure ASGI rather than ``BaseHTTPMiddleware``: it never touches a response body, so the agent's
SSE streams pass through unbuffered; a renewed session's cookie rides the response start. With
``AUTH_MODE=none`` it hands every request straight to the app — the same ``receive`` and
``send`` it was given — and does nothing else at all.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

from epicurus_core_app.auth.config import origin_of
from epicurus_core_app.auth.service import (
    AUTH_PREFIX,
    SESSION_COOKIE,
    AuthService,
    cookie_header,
    read_cookie,
)

__all__ = ["PROXY_HEADERS", "AuthMiddleware"]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The headers whose presence means "this request came through a proxy".
PROXY_HEADERS: Final = frozenset(
    {b"x-forwarded-for", b"forwarded", b"x-forwarded-host", b"x-forwarded-proto", b"x-real-ip"}
)
_UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAME_ORIGIN_FETCH: Final = frozenset({"same-origin", "none"})

_UNAUTHENTICATED: Final = {"detail": "Sign in to continue.", "code": "unauthenticated"}
_CROSS_SITE: Final = {"detail": "Cross-site request refused.", "code": "cross_site"}


def _exempt(path: str) -> bool:
    """``/health`` and the sign-in endpoints — never a dot-segment path dressed up as one."""
    if path == "/health":
        return True
    if not path.startswith(f"{AUTH_PREFIX}/"):
        return False
    return ".." not in path.split("/")


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == name
    ]


class AuthMiddleware:
    """Require a session on proxied requests when sign-in is on; a no-op when it is off."""

    def __init__(self, app: ASGIApp, *, service: AuthService) -> None:
        self.app = app
        self._service = service
        self._enabled = service.config.enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._enabled or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        names = {key.lower() for key, _ in scope.get("headers", [])}
        if not names & PROXY_HEADERS:
            await self.app(scope, receive, send)
            return

        if (
            scope["type"] == "http"
            and scope["method"] in _UNSAFE_METHODS
            and self._cross_site(scope)
        ):
            await _json(send, 403, _CROSS_SITE)
            return
        if _exempt(scope["path"]):
            await self.app(scope, receive, send)
            return

        token = read_cookie(_header_values(scope, b"cookie"), SESSION_COOKIE)
        resolved = await self._service.sessions.resolve(token)
        if resolved is None:
            if scope["type"] == "websocket":
                # Refusing before accept makes the server answer the handshake with a 403.
                await send({"type": "websocket.close", "code": 1008})
                return
            await _json(send, 401, _UNAUTHENTICATED)
            return
        scope.setdefault("state", {})["auth"] = resolved.identity
        if not resolved.renewed or scope["type"] != "http" or token is None:
            await self.app(scope, receive, send)
            return

        renewed_cookie = cookie_header(
            SESSION_COOKIE,
            token,
            max_age=self._service.sessions.max_age,
            path="/",
            secure=self._service.config.secure_cookies,
        ).encode("latin-1")

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"set-cookie", renewed_cookie)]
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    def _cross_site(self, scope: Scope) -> bool:
        fetch_site = _header_values(scope, b"sec-fetch-site")
        if fetch_site:
            return fetch_site[0].strip().lower() not in _SAME_ORIGIN_FETCH
        origins = _header_values(scope, b"origin")
        if origins:
            return origin_of(origins[0]) != self._service.config.public_origin
        return False


async def _json(send: Send, status: int, body: dict[str, str]) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
