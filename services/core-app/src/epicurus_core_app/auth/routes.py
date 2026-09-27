"""``/platform/v1/auth`` — the four sign-in endpoints (#969).

    GET  /platform/v1/auth/session   — always 200: the mode, and who is signed in (if anyone)
    GET  /platform/v1/auth/login     — 302 to the provider (sets the transaction cookie)
    GET  /platform/v1/auth/callback  — 302 to ``next`` on success, ``/?auth_error=<code>`` on
                                       any failure (a top-level navigation never ends on JSON)
    POST /platform/v1/auth/logout    — 200 ``{"signed_out": true}``, always

Every response carries ``Cache-Control: no-store``. With ``AUTH_MODE=none`` the session
endpoint says so, login and callback answer 404 ``auth_disabled``, and logout still answers 200
(a shell that signs out of a deployment that has no sign-in is not an error).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from epicurus_core import get_logger
from epicurus_core_app.auth.errors import AuthFlowError
from epicurus_core_app.auth.service import (
    AUTH_PREFIX,
    LOGIN_TTL,
    SESSION_COOKIE,
    TX_COOKIE,
    AuthService,
    ResolvedSession,
    clear_cookie_header,
    cookie_header,
    read_cookie,
)

__all__ = ["create_auth_router"]

log = get_logger("epicurus_core_app.auth")

_NO_STORE = {"Cache-Control": "no-store"}
_DISABLED = {"detail": "Sign-in is not enabled on this deployment.", "code": "auth_disabled"}


def _cookies(request: Request) -> list[str]:
    return request.headers.getlist("cookie")


def create_auth_router(service: AuthService) -> APIRouter:
    """Build the ``/platform/v1/auth`` router over one :class:`AuthService`."""
    router = APIRouter(prefix=AUTH_PREFIX, tags=["auth"])
    config = service.config
    secure = config.secure_cookies

    def _session_cookie(response: Response, token: str) -> None:
        response.headers.append(
            "set-cookie",
            cookie_header(
                SESSION_COOKIE, token, max_age=service.sessions.max_age, path="/", secure=secure
            ),
        )

    def _clear_tx(response: Response) -> None:
        response.headers.append(
            "set-cookie", clear_cookie_header(TX_COOKIE, path=AUTH_PREFIX, secure=secure)
        )

    def _error_redirect(exc: AuthFlowError) -> RedirectResponse:
        service.refuse(exc)
        response = RedirectResponse(
            url=f"/?auth_error={exc.code}", status_code=302, headers=_NO_STORE
        )
        _clear_tx(response)
        return response

    @router.get("/session")
    async def session(request: Request) -> JSONResponse:
        """Whether sign-in is on, and who is signed in — 200 whatever the answer."""
        resolved: ResolvedSession | None = None
        token = read_cookie(_cookies(request), SESSION_COOKIE)
        if config.enabled and token:
            try:
                resolved = await service.sessions.resolve(token)
            except Exception as exc:  # the question still has an answer: not signed in
                log.error("session lookup failed", error=str(exc))
        body: dict[str, Any] = service.session_view(resolved)
        response = JSONResponse(body, headers=_NO_STORE)
        if resolved is not None and resolved.renewed and token is not None:
            _session_cookie(response, token)
        return response

    @router.get("/login", response_model=None)
    async def login(next_: str | None = Query(default=None, alias="next")) -> Response:
        """Send the browser to the provider, remembering where to come back to."""
        if not config.enabled:
            return JSONResponse(_DISABLED, status_code=404, headers=_NO_STORE)
        try:
            url, state = await service.begin_login(next_)
        except AuthFlowError as exc:
            return _error_redirect(exc)
        response = RedirectResponse(url=url, status_code=302, headers=_NO_STORE)
        response.headers.append(
            "set-cookie",
            cookie_header(
                TX_COOKIE,
                state,
                max_age=int(LOGIN_TTL.total_seconds()),
                path=AUTH_PREFIX,
                secure=secure,
            ),
        )
        return response

    @router.get("/callback", response_model=None)
    async def callback(request: Request) -> Response:
        """The provider's redirect back: finish the sign-in, or say why not — always a 302."""
        if not config.enabled:
            return JSONResponse(_DISABLED, status_code=404, headers=_NO_STORE)
        params = dict(request.query_params.items())
        tx_state = read_cookie(_cookies(request), TX_COOKIE)
        try:
            completed = await service.complete_login(params, tx_state=tx_state)
        except AuthFlowError as exc:
            return _error_redirect(exc)
        except Exception as exc:  # never a 500 page at the end of a top-level navigation
            log.error("sign-in failed unexpectedly", error=f"{type(exc).__name__}: {exc}")
            return _error_redirect(
                AuthFlowError("misconfigured", f"unexpected {type(exc).__name__}")
            )
        response = RedirectResponse(url=completed.next_path, status_code=302, headers=_NO_STORE)
        _clear_tx(response)
        _session_cookie(response, completed.token)
        return response

    @router.post("/logout")
    async def logout(request: Request) -> JSONResponse:
        """End this browser's session (if it has one) and clear the cookie."""
        token = read_cookie(_cookies(request), SESSION_COOKIE)
        if config.enabled and token:
            try:
                await service.logout(token)
            except Exception as exc:  # the cookie is cleared regardless; the row expires
                log.error("sign-out could not delete the session", error=str(exc))
        response = JSONResponse({"signed_out": True}, headers=_NO_STORE)
        response.headers.append(
            "set-cookie", clear_cookie_header(SESSION_COOKIE, path="/", secure=secure)
        )
        return response

    return router
