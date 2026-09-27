"""Sign-in, sessions and sign-out — the flow the routes and the middleware share (#969).

:class:`SessionManager` turns a cookie into a session: it hashes the token, looks it up (through
a short in-process cache, so a chat that polls does not cost a query per request), refuses an
expired one, and slides the expiry forward at most once an hour while the session is in use.
:class:`AuthService` runs the login: it starts one (PKCE + ``state`` + ``nonce``, a login row, a
redirect to the provider), completes one (the transaction cookie against the stored row, the
code exchange, the ID token, userinfo, the admission rule, a new session), and ends one.

Every terminal outcome of a sign-in is counted in ``epicurus_core_auth_sign_ins_total`` and
logged — INFO for a sign-in, WARNING for a refusal with the reason — and none of those lines
carries a code, a token, a cookie, a nonce, a verifier or a secret.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from prometheus_client import Counter

from epicurus_core import get_logger
from epicurus_core_app.auth.admission import admit, claim_email, claim_groups
from epicurus_core_app.auth.config import AuthConfig
from epicurus_core_app.auth.errors import AuthFlowError
from epicurus_core_app.auth.oidc import OidcClient, code_challenge_for, new_code_verifier
from epicurus_core_app.auth.store import NEXT_PATH_LEN, AuthStore, SessionRecord, hash_token

__all__ = [
    "AUTH_PREFIX",
    "LOGIN_TTL",
    "SESSION_COOKIE",
    "SIGN_INS",
    "TX_COOKIE",
    "AuthIdentity",
    "AuthService",
    "CompletedLogin",
    "ResolvedSession",
    "SessionManager",
    "clear_cookie_header",
    "cookie_header",
    "read_cookie",
    "safe_next",
]

log = get_logger("epicurus_core_app.auth")

SESSION_COOKIE: Final = "epicurus_session"
TX_COOKIE: Final = "epicurus_auth_tx"
AUTH_PREFIX: Final = "/platform/v1/auth"
LOGIN_TTL: Final = timedelta(minutes=10)
#: A session's expiry slides forward at most this often (one UPDATE an hour, not per request).
RENEW_AFTER: Final = timedelta(hours=1)
#: How long a validated session is trusted from memory before the row is read again.
CACHE_TTL_S: Final = 60.0

SIGN_INS = Counter(
    "epicurus_core_auth_sign_ins_total",
    "Sign-ins through the OpenID Connect provider, by outcome (`ok` or the auth_error code).",
    ["tenant", "outcome"],
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── cookies ───────────────────────────────────────────────────────────────────


def cookie_header(name: str, value: str, *, max_age: int, path: str, secure: bool) -> str:
    """A ``Set-Cookie`` value: always HttpOnly and SameSite=Lax; Secure when asked."""
    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={path}", "HttpOnly", "SameSite=Lax"]
    if max_age <= 0:
        parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header(name: str, *, path: str, secure: bool) -> str:
    """A ``Set-Cookie`` value that deletes *name* at *path*."""
    return cookie_header(name, "", max_age=0, path=path, secure=secure)


def read_cookie(headers: list[str], name: str) -> str | None:
    """The value of cookie *name* across every ``Cookie`` header, tolerant of junk around it."""
    for header in headers:
        for part in header.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == name:
                value = value.strip().strip('"')
                return value or None
    return None


# ── the `next` parameter ──────────────────────────────────────────────────────


def safe_next(raw: str | None) -> str:
    """*raw* if it is a same-origin path worth returning to after sign-in, else ``/``.

    Refused: anything not starting with one ``/`` (``//host`` and ``/\\host`` are how a
    "path" becomes another origin in a browser), anything with a scheme or host, control
    characters or backslashes, an overlong value, and anything under the sign-in endpoints
    themselves (percent-decoded first, so ``/platform/v1/%61uth/logout`` is caught too).
    """
    if not raw or len(raw) > NEXT_PATH_LEN or not raw.startswith("/"):
        return "/"
    if raw.startswith("//") or "\\" in raw:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return "/"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    decoded = unquote(parts.path)
    if decoded.startswith("//") or "\\" in decoded:
        return "/"
    if decoded == AUTH_PREFIX or decoded.startswith(f"{AUTH_PREFIX}/"):
        return "/"
    return raw


# ── sessions ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthIdentity:
    """Who a request is from — put on ``request.state.auth`` for a signed-in proxied request."""

    tenant: str
    subject: str
    email: str | None
    name: str | None
    groups: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedSession:
    """A valid session, and whether resolving it slid its expiry (so the cookie is re-issued)."""

    record: SessionRecord
    renewed: bool

    @property
    def identity(self) -> AuthIdentity:
        record = self.record
        return AuthIdentity(
            tenant=record.tenant,
            subject=record.subject,
            email=record.email,
            name=record.name,
            groups=record.groups,
            expires_at=record.expires_at,
        )


class SessionManager:
    """Cookie token → session, with expiry, sliding renewal and a short in-process cache."""

    def __init__(
        self,
        store: AuthStore,
        *,
        tenant: str,
        session_days: int,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._tenant = tenant
        self._lifetime = timedelta(days=session_days)
        self._clock = clock
        self._monotonic = monotonic
        self._cache: dict[str, tuple[SessionRecord, float]] = {}

    @property
    def max_age(self) -> int:
        """The session cookie's ``Max-Age`` — the full sliding lifetime, in seconds."""
        return int(self._lifetime.total_seconds())

    async def create(
        self,
        *,
        issuer: str,
        subject: str,
        email: str | None,
        name: str | None,
        groups: list[str],
    ) -> tuple[str, SessionRecord]:
        """A new session: returns the opaque token (for the cookie) and the stored record."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        record = await self._store.add_session(
            token_hash=hash_token(token),
            tenant=self._tenant,
            issuer=issuer,
            subject=subject,
            email=email,
            name=name,
            groups=groups,
            now=now,
            expires_at=now + self._lifetime,
        )
        return token, record

    async def resolve(self, token: str | None) -> ResolvedSession | None:
        """The live session for *token*, sliding it forward if it is due; else ``None``."""
        if not token:
            return None
        token_hash = hash_token(token)
        now = self._clock()
        record = self._cached(token_hash)
        if record is None:
            record = await self._store.get_session(tenant=self._tenant, token_hash=token_hash)
            if record is None:
                return None
        if record.expires_at <= now:
            self._cache.pop(token_hash, None)
            return None
        renewed = False
        if now - record.last_seen_at >= RENEW_AFTER:
            expires_at = now + self._lifetime
            try:
                still_there = await self._store.touch_session(
                    tenant=self._tenant,
                    token_hash=token_hash,
                    last_seen_at=now,
                    expires_at=expires_at,
                )
            except Exception as exc:  # a renewal that fails leaves a valid session valid
                log.warning("session renewal failed; the session keeps its expiry", error=str(exc))
            else:
                if not still_there:  # signed out elsewhere between the read and the write
                    self._cache.pop(token_hash, None)
                    return None
                record = _slid(record, last_seen_at=now, expires_at=expires_at)
                renewed = True
        self._cache[token_hash] = (record, self._monotonic())
        return ResolvedSession(record=record, renewed=renewed)

    async def revoke(self, token: str | None) -> SessionRecord | None:
        """Delete the session for *token*, if any; returns what it was (for the log line)."""
        if not token:
            return None
        token_hash = hash_token(token)
        self._cache.pop(token_hash, None)
        record = await self._store.get_session(tenant=self._tenant, token_hash=token_hash)
        await self._store.delete_session(tenant=self._tenant, token_hash=token_hash)
        return record

    def _cached(self, token_hash: str) -> SessionRecord | None:
        entry = self._cache.get(token_hash)
        if entry is None:
            return None
        record, cached_at = entry
        if self._monotonic() - cached_at >= CACHE_TTL_S:
            del self._cache[token_hash]
            return None
        return record


def _slid(record: SessionRecord, *, last_seen_at: datetime, expires_at: datetime) -> SessionRecord:
    return SessionRecord(
        id=record.id,
        tenant=record.tenant,
        issuer=record.issuer,
        subject=record.subject,
        email=record.email,
        name=record.name,
        groups=record.groups,
        created_at=record.created_at,
        last_seen_at=last_seen_at,
        expires_at=expires_at,
    )


# ── the login flow ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompletedLogin:
    """A finished sign-in: the token for the cookie, the session, and where to go next."""

    token: str
    record: SessionRecord
    next_path: str

    def __repr__(self) -> str:
        return f"CompletedLogin(subject={self.record.subject!r}, next_path={self.next_path!r})"


def _display_name(claims: Mapping[str, Any]) -> str | None:
    for claim in ("name", "preferred_username"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class AuthService:
    """The sign-in flow, end to end, for one configured provider."""

    def __init__(
        self,
        config: AuthConfig,
        *,
        store: AuthStore,
        oidc: OidcClient,
        sessions: SessionManager,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config
        self.store = store
        self.oidc = oidc
        self.sessions = sessions
        self._clock = clock

    def _count(self, outcome: str) -> None:
        SIGN_INS.labels(tenant=self.config.tenant, outcome=outcome).inc()

    def refuse(self, exc: AuthFlowError) -> None:
        """Count and log a failed sign-in (the caller redirects with ``exc.code``)."""
        self._count(exc.code)
        known: dict[str, Any] = {}
        claims = exc.claims
        if claims:
            known = {"subject": claims.get("sub"), "email": claim_email(claims)}
        log.warning("sign-in refused", reason=exc.code, detail=exc.message, **known)

    # ── /session ─────────────────────────────────────────────────────────────────

    def session_view(self, resolved: ResolvedSession | None) -> dict[str, Any]:
        """The ``GET /platform/v1/auth/session`` body for *resolved* (``None`` = signed out)."""
        config = self.config
        user: dict[str, Any] | None = None
        expires_at: str | None = None
        if resolved is not None:
            record = resolved.record
            user = {
                "subject": record.subject,
                "email": record.email,
                "name": record.name,
                "groups": list(record.groups),
            }
            expires_at = record.expires_at.astimezone(UTC).isoformat()
        # With sign-in off the presentation knobs are inert whatever the environment says: a
        # stray OIDC_AUTO_REDIRECT=true beside AUTH_MODE=none must not reach the shell.
        return {
            "mode": config.mode,
            "signed_in": resolved is not None,
            "provider_name": config.provider_name if config.enabled else None,
            "auto_redirect": config.auto_redirect if config.enabled else False,
            "user": user,
            "expires_at": expires_at,
        }

    # ── /login ───────────────────────────────────────────────────────────────────

    async def begin_login(self, next_raw: str | None) -> tuple[str, str]:
        """Start a sign-in: returns ``(provider URL to redirect to, state for the tx cookie)``."""
        meta = await self.oidc.metadata()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = new_code_verifier()
        now = self._clock()
        try:
            await self.store.add_login_state(
                tenant=self.config.tenant,
                state=state,
                nonce=nonce,
                code_verifier=verifier,
                next_path=safe_next(next_raw),
                now=now,
                expires_at=now + LOGIN_TTL,
            )
        except Exception as exc:
            raise AuthFlowError(
                "misconfigured", f"the login could not be recorded: {type(exc).__name__}"
            ) from exc
        url = self.oidc.authorization_url(
            meta, state=state, nonce=nonce, code_challenge=code_challenge_for(verifier)
        )
        return url, state

    # ── /callback ────────────────────────────────────────────────────────────────

    async def complete_login(
        self, params: Mapping[str, str], *, tx_state: str | None
    ) -> CompletedLogin:
        """Finish a sign-in from the provider's callback, or raise :class:`AuthFlowError`."""
        state = params.get("state") or None
        login = None
        if state and tx_state and hmac.compare_digest(state.encode(), tx_state.encode()):
            # Consumed before anything else can fail: a login is single-use whatever happens.
            try:
                login = await self.store.take_login_state(tenant=self.config.tenant, state=state)
            except Exception as exc:
                raise AuthFlowError(
                    "misconfigured", f"the login could not be read back: {type(exc).__name__}"
                ) from exc

        error = params.get("error")
        if error:
            if error == "access_denied":
                raise AuthFlowError("access_denied", "the provider reported access_denied")
            raise AuthFlowError("provider_error", f"the provider reported an error ({error[:64]})")
        if login is None:
            raise AuthFlowError(
                "state_mismatch",
                "no login matches this callback (missing, unknown, replayed, or another browser's)",
            )
        if login.expires_at <= self._clock():
            raise AuthFlowError("state_mismatch", "the login expired before the provider returned")
        code = params.get("code")
        if not code:
            raise AuthFlowError(
                "provider_error", "the callback carries neither a code nor an error"
            )

        meta = await self.oidc.metadata()
        callback_iss = params.get("iss")
        if callback_iss is not None and callback_iss != meta.issuer:
            raise AuthFlowError(
                "invalid_token", "the callback's iss parameter is not the provider's issuer"
            )
        if callback_iss is None and meta.iss_parameter_supported:
            raise AuthFlowError(
                "invalid_token",
                "the provider promises an iss parameter (RFC 9207) and the callback has none",
            )

        claims = await self.oidc.authenticate(
            meta, code=code, code_verifier=login.code_verifier, nonce=login.nonce
        )
        refusal = admit(claims, self.config)
        if refusal is not None:
            error_text = {
                "email_unverified": "the allowlisted email is not verified by the provider",
                "groups_claim_missing": "a groups allowlist is set and no groups claim arrived "
                "(is `groups` among OIDC_SCOPES?)",
            }.get(refusal, "no admission rule admits this identity")
            raise AuthFlowError(refusal, error_text, claims=claims)

        try:
            token, record = await self.sessions.create(
                issuer=str(claims.get("iss", meta.issuer)),
                subject=str(claims["sub"]),
                email=claim_email(claims),
                name=_display_name(claims),
                groups=claim_groups(claims) or [],
            )
        except Exception as exc:
            raise AuthFlowError(
                "misconfigured", f"the session could not be stored: {type(exc).__name__}"
            ) from exc
        self._count("ok")
        log.info("signed in", subject=record.subject, email=record.email)
        return CompletedLogin(token=token, record=record, next_path=login.next_path)

    # ── /logout ──────────────────────────────────────────────────────────────────

    async def logout(self, token: str | None) -> None:
        """End the session behind *token*, if there is one. Never raises for "no session"."""
        record = await self.sessions.revoke(token)
        if record is not None:
            log.info("signed out", subject=record.subject, email=record.email)
