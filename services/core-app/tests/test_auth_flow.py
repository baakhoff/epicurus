"""Sign-in (#969) end to end, against a fake OpenID Connect provider.

The provider is an ``httpx.MockTransport`` with locally generated RSA and EC keys: it serves
discovery and a JWKS, runs a real code exchange (it checks the PKCE verifier and the redirect
URI), signs real ID tokens and answers userinfo. The browser is an ``httpx.AsyncClient`` on an
``ASGITransport`` carrying ``X-Forwarded-For`` the way the web shell's nginx does, so the
middleware treats it as a proxied request; a second client without the header plays a module.

Covered here: the happy path through a minimal app and through the real ``create_app``; every
refusal code the callback can redirect with; the ID-token checks one by one (signature, alg,
``iss``/``aud``/``azp``, ``exp``, nonce, the RFC 9207 ``iss`` parameter); key rotation and its
rate limit; client authentication (basic / post / public); userinfo merging; ``next`` open
redirects; sessions (sliding renewal, expiry, logout); the middleware matrix (proxied vs direct,
none vs oidc, exempt paths, the cross-site check); SSE through the middleware, unbuffered.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core_app.auth import AuthConfigError, build_auth
from epicurus_core_app.auth.config import load_auth_config
from epicurus_core_app.auth.middleware import AuthMiddleware, _exempt
from epicurus_core_app.auth.oidc import OidcClient, code_challenge_for
from epicurus_core_app.auth.routes import create_auth_router
from epicurus_core_app.auth.service import AuthService, SessionManager
from epicurus_core_app.auth.store import AuthStore, _AuthSessionRow
from epicurus_core_app.settings import CoreAppSettings

ISSUER = "https://id.example.test"
PUBLIC = "http://localhost:8084"
CLIENT_ID = "epicurus"
XFF = {"x-forwarded-for": "203.0.113.7"}

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
STRANGER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    if isinstance(key, rsa.RSAPrivateKey):
        jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    else:
        jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, use="sig")
    return jwk


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class FakeProvider:
    """An OpenID Connect provider in an ``httpx.MockTransport``. Mutable per test."""

    def __init__(self) -> None:
        self.discovery: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
            "response_types_supported": ["code"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        }
        self.keys: list[dict[str, Any]] = [
            _public_jwk(RSA_KEY, "rsa-1"),
            _public_jwk(EC_KEY, "ec-1"),
        ]
        self.sign_key: Any = RSA_KEY
        self.sign_kid: str | None = "rsa-1"
        self.sign_alg = "RS256"
        self.claims: dict[str, Any] = {
            "sub": "user-1",
            "email": "Me@Example.com",
            "email_verified": True,
            "name": "Me",
        }
        #: Applied last; a ``None`` value removes the claim.
        self.overrides: dict[str, Any] = {}
        self.forge: Callable[[dict[str, Any]], str] | None = None
        self.userinfo: dict[str, Any] | None = None
        self.userinfo_status = 200
        self.token_status = 200
        self.down: set[str] = set()
        self.grants: dict[str, dict[str, str]] = {}
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def hits(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)

    def authorize(self, location: str) -> tuple[str, dict[str, str]]:
        """What the provider does when the browser arrives: issue a code for this request."""
        query = dict(parse_qsl(urlsplit(location).query))
        code = secrets.token_urlsafe(8)
        self.grants[code] = query
        return code, query

    def id_token(self, grant: dict[str, str]) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.discovery["issuer"],
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": grant["nonce"],
            **self.claims,
        }
        for name, value in self.overrides.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        if self.forge is not None:
            return self.forge(claims)
        headers = {"kid": self.sign_kid} if self.sign_kid else None
        return jwt.encode(claims, self.sign_key, algorithm=self.sign_alg, headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.down:
            raise httpx.ConnectError("provider down", request=request)
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery)
        if path == "/jwks":
            return httpx.Response(200, json={"keys": self.keys})
        if path == "/token":
            form = dict(parse_qsl(request.content.decode()))
            grant = self.grants.pop(form.get("code", ""), None)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            if (
                grant is None
                or code_challenge_for(form.get("code_verifier", "")) != grant["code_challenge"]
                or form.get("redirect_uri") != grant["redirect_uri"]
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "id_token": self.id_token(grant),
                    "access_token": "at",
                    "token_type": "Bearer",
                },
            )
        if path == "/userinfo":
            if self.userinfo is None:
                return httpx.Response(404)
            return httpx.Response(self.userinfo_status, json=self.userinfo)
        return httpx.Response(404)


class Clock:
    """A settable wall clock and monotonic clock, starting now (ID tokens use real time)."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)
        self.mono = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, delta: timedelta) -> None:
        self.now += delta
        self.mono += delta.total_seconds()


@dataclass
class Harness:
    provider: FakeProvider
    service: AuthService
    engine: AsyncEngine
    clock: Clock
    client: httpx.AsyncClient  # the browser, through the proxy
    direct: httpx.AsyncClient  # a module on the internal network
    app: FastAPI
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def _test_app(service: AuthService) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware, service=service)
    app.include_router(create_auth_router(service))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/platform/v1/things")
    async def things(request: Request) -> dict[str, Any]:
        identity = getattr(request.state, "auth", None)
        return {"subject": identity.subject if identity else None}

    @app.post("/platform/v1/things")
    async def change() -> dict[str, bool]:
        return {"changed": True}

    @app.get("/platform/v1/stream")
    async def stream() -> StreamingResponse:
        async def events() -> AsyncIterator[bytes]:
            for i in range(3):
                yield f"data: {i}\n\n".encode()

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


Builder = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def build(tmp_path: Path) -> AsyncIterator[Builder]:
    made: list[Harness] = []

    async def _build(provider: FakeProvider | None = None, **overrides: Any) -> Harness:
        provider = provider or FakeProvider()
        values: dict[str, Any] = {
            "auth_mode": "oidc",
            "oidc_issuer_url": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_allowed_emails": "me@example.com",
            "oauth_redirect_base_url": PUBLIC,
        }
        values.update(overrides)
        config = load_auth_config(CoreAppSettings(**values))
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'auth{len(made)}.db'}")
        store = AuthStore(engine)
        await store.init()
        clock = Clock()
        service = AuthService(
            config,
            store=store,
            oidc=OidcClient(config, transport=provider.transport, monotonic=clock.monotonic),
            sessions=SessionManager(
                store,
                tenant=config.tenant,
                session_days=config.session_days,
                clock=clock,
                monotonic=clock.monotonic,
            ),
            clock=clock,
        )
        app = _test_app(service)
        transport = httpx.ASGITransport(app=app)
        harness = Harness(
            provider=provider,
            service=service,
            engine=engine,
            clock=clock,
            client=httpx.AsyncClient(transport=transport, base_url=PUBLIC, headers=XFF),
            direct=httpx.AsyncClient(transport=transport, base_url="http://core-app:8080"),
            app=app,
        )
        made.append(harness)
        return harness

    yield _build
    for harness in made:
        await harness.client.aclose()
        await harness.direct.aclose()
        await harness.engine.dispose()


async def _login(h: Harness, next_: str = "/chat") -> tuple[str, dict[str, str]]:
    response = await h.client.get("/platform/v1/auth/login", params={"next": next_})
    assert response.status_code == 302, response.text
    return h.provider.authorize(response.headers["location"])


async def _sign_in(h: Harness, next_: str = "/chat") -> httpx.Response:
    code, query = await _login(h, next_)
    return await h.client.get(
        "/platform/v1/auth/callback", params={"code": code, "state": query["state"]}
    )


def _set_cookies(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _cookie(response: httpx.Response, name: str) -> str | None:
    return next((c for c in _set_cookies(response) if c.startswith(f"{name}=")), None)


def _error(response: httpx.Response) -> str | None:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    if not location.startswith("/?auth_error="):
        return None
    return location.removeprefix("/?auth_error=")


def _count(outcome: str) -> float:
    value = REGISTRY.get_sample_value(
        "epicurus_core_auth_sign_ins_total", {"tenant": "local", "outcome": outcome}
    )
    return value or 0.0


# ── the happy path ────────────────────────────────────────────────────────────


async def test_a_full_sign_in_sets_a_session_that_opens_the_door(build: Builder) -> None:
    h = await build()
    assert (await h.client.get("/platform/v1/things")).status_code == 401
    before = _count("ok")

    login = await h.client.get("/platform/v1/auth/login", params={"next": "/chat?s=1"})
    assert login.status_code == 302
    assert login.headers["cache-control"] == "no-store"
    location = urlsplit(login.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
    query = dict(parse_qsl(location.query))
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{PUBLIC}/platform/v1/auth/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256"
    assert query["state"] and query["nonce"] and query["code_challenge"]
    tx = _cookie(login, "epicurus_auth_tx")
    assert tx is not None and f"epicurus_auth_tx={query['state']}" in tx
    assert "Path=/platform/v1/auth" in tx and "HttpOnly" in tx and "SameSite=Lax" in tx
    assert "Max-Age=600" in tx and "Secure" not in tx

    code, _ = h.provider.authorize(login.headers["location"])
    callback = await h.client.get(
        "/platform/v1/auth/callback", params={"code": code, "state": query["state"]}
    )
    assert callback.status_code == 302 and callback.headers["location"] == "/chat?s=1"
    assert callback.headers["cache-control"] == "no-store"
    session_cookie = _cookie(callback, "epicurus_session")
    assert session_cookie is not None
    assert "Path=/;" in session_cookie and "HttpOnly" in session_cookie
    assert "SameSite=Lax" in session_cookie and f"Max-Age={30 * 86400}" in session_cookie
    cleared = _cookie(callback, "epicurus_auth_tx")
    assert cleared is not None and "Max-Age=0" in cleared

    things = await h.client.get("/platform/v1/things")
    assert things.status_code == 200 and things.json() == {"subject": "user-1"}

    session = await h.client.get("/platform/v1/auth/session")
    assert session.headers["cache-control"] == "no-store"
    body = session.json()
    assert body["mode"] == "oidc" and body["signed_in"] is True
    assert body["provider_name"] is None and body["auto_redirect"] is False
    assert body["user"] == {
        "subject": "user-1",
        "email": "me@example.com",
        "name": "Me",
        "groups": [],
    }
    expires = datetime.fromisoformat(body["expires_at"])
    assert expires - h.clock.now == timedelta(days=30)
    assert _count("ok") == before + 1

    # The cookie is the token; the table holds only its hash.
    token = h.client.cookies.get("epicurus_session")
    async with h.engine.connect() as conn:
        ids = (await conn.execute(select(_AuthSessionRow.id))).scalars().all()
    assert token is not None and token not in ids and len(ids) == 1 and len(ids[0]) == 64


async def test_the_session_endpoint_describes_a_signed_out_browser(build: Builder) -> None:
    h = await build(oidc_provider_name="Pocket ID", oidc_auto_redirect=True)
    body = (await h.client.get("/platform/v1/auth/session")).json()
    assert body == {
        "mode": "oidc",
        "signed_in": False,
        "provider_name": "Pocket ID",
        "auto_redirect": True,
        "user": None,
        "expires_at": None,
    }


async def test_a_secure_public_url_makes_secure_cookies(build: Builder) -> None:
    h = await build(oauth_redirect_base_url="https://epicurus.example.com")
    login = await h.client.get("/platform/v1/auth/login")
    tx = _cookie(login, "epicurus_auth_tx")
    assert tx is not None and tx.endswith("Secure")


# ── client authentication at the token endpoint ───────────────────────────────


async def test_a_public_client_sends_its_id_in_the_body_and_nothing_else(build: Builder) -> None:
    h = await build()
    assert (await _sign_in(h)).headers["location"] == "/chat"
    token_call = next(r for r in h.provider.requests if r.url.path == "/token")
    form = dict(parse_qsl(token_call.content.decode()))
    assert form["client_id"] == CLIENT_ID and "client_secret" not in form
    assert "authorization" not in token_call.headers
    assert form["grant_type"] == "authorization_code" and form["code_verifier"]


async def test_a_confidential_client_uses_basic_auth_by_default(build: Builder) -> None:
    h = await build(oidc_client_secret="s3cr+t/=")
    assert (await _sign_in(h)).headers["location"] == "/chat"
    token_call = next(r for r in h.provider.requests if r.url.path == "/token")
    scheme, _, encoded = token_call.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "epicurus:s3cr%2Bt%2F%3D"
    assert "client_secret" not in dict(parse_qsl(token_call.content.decode()))


async def test_client_secret_post_when_it_is_the_only_method(build: Builder) -> None:
    provider = FakeProvider()
    provider.discovery["token_endpoint_auth_methods_supported"] = ["client_secret_post"]
    h = await build(provider, oidc_client_secret="s3cret")
    assert (await _sign_in(h)).headers["location"] == "/chat"
    token_call = next(r for r in h.provider.requests if r.url.path == "/token")
    form = dict(parse_qsl(token_call.content.decode()))
    assert form["client_secret"] == "s3cret" and "authorization" not in token_call.headers


# ── keys ──────────────────────────────────────────────────────────────────────


async def test_an_ec_signed_token_is_accepted(build: Builder) -> None:
    provider = FakeProvider()
    provider.sign_key, provider.sign_kid, provider.sign_alg = EC_KEY, "ec-1", "ES256"
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"


async def test_a_rotated_key_is_fetched_once_and_then_rate_limited(build: Builder) -> None:
    provider = FakeProvider()
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"
    assert provider.hits("/jwks") == 1

    # The provider rotates: a new key id the cached JWKS has never seen.
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider.keys.append(_public_jwk(new_key, "rsa-2"))
    provider.sign_key, provider.sign_kid = new_key, "rsa-2"
    h.clock.advance(timedelta(seconds=31))
    assert (await _sign_in(h)).headers["location"] == "/chat"
    assert provider.hits("/jwks") == 2

    # A token naming yet another unknown kid within 30s does not buy another fetch.
    provider.sign_kid = "rsa-3"
    assert _error(await _sign_in(h)) == "invalid_token"
    assert provider.hits("/jwks") == 2


async def test_a_token_without_a_kid_is_verified_against_every_candidate(build: Builder) -> None:
    provider = FakeProvider()
    provider.keys.insert(0, _public_jwk(STRANGER_KEY, "other"))
    provider.sign_kid = None
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"


# ── every refusal ─────────────────────────────────────────────────────────────


async def test_an_unreachable_provider_is_named_at_login_and_startup_did_not_mind(
    build: Builder,
) -> None:
    provider = FakeProvider()
    provider.down.add("/.well-known/openid-configuration")
    h = await build(provider)  # built fine: discovery is lazy
    before = _count("provider_unreachable")
    response = await h.client.get("/platform/v1/auth/login")
    assert _error(response) == "provider_unreachable"
    assert response.headers["cache-control"] == "no-store"
    assert _count("provider_unreachable") == before + 1
    provider.down.clear()  # it comes back, and a failure was never cached
    assert (await _sign_in(h)).headers["location"] == "/chat"


async def test_a_discovery_document_for_another_issuer_is_misconfigured(build: Builder) -> None:
    provider = FakeProvider()
    provider.discovery["issuer"] = "https://evil.example.test"
    h = await build(provider)
    assert _error(await h.client.get("/platform/v1/auth/login")) == "misconfigured"


async def test_one_trailing_slash_on_the_issuer_is_tolerated(build: Builder) -> None:
    provider = FakeProvider()
    provider.discovery["issuer"] = f"{ISSUER}/"
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"


@pytest.mark.parametrize(
    "change",
    [
        {"id_token_signing_alg_values_supported": ["HS256", "none"]},
        {"response_types_supported": ["token"]},
        {"code_challenge_methods_supported": ["plain"]},
        {"jwks_uri": None},
    ],
)
async def test_metadata_this_client_cannot_use_is_misconfigured(
    build: Builder, change: dict[str, Any]
) -> None:
    provider = FakeProvider()
    provider.discovery.update(change)
    h = await build(provider)
    assert _error(await h.client.get("/platform/v1/auth/login")) == "misconfigured"


async def test_access_denied_is_passed_on_and_consumes_the_login(build: Builder) -> None:
    h = await build()
    _, query = await _login(h)
    denied = await h.client.get(
        "/platform/v1/auth/callback", params={"error": "access_denied", "state": query["state"]}
    )
    assert _error(denied) == "access_denied"
    cleared = _cookie(denied, "epicurus_auth_tx")
    assert cleared is not None and "Max-Age=0" in cleared
    assert _cookie(denied, "epicurus_session") is None
    assert await h.service.store.take_login_state(tenant="local", state=query["state"]) is None


@pytest.mark.parametrize("params", [{"error": "server_error"}, {"error": "login_required"}, {}])
async def test_any_other_provider_answer_is_a_provider_error(
    build: Builder, params: dict[str, str]
) -> None:
    h = await build()
    _, query = await _login(h)
    response = await h.client.get(
        "/platform/v1/auth/callback", params={**params, "state": query["state"]}
    )
    assert _error(response) == "provider_error"


async def test_a_callback_without_the_transaction_cookie_is_a_state_mismatch(
    build: Builder,
) -> None:
    h = await build()
    code, query = await _login(h)
    h.client.cookies.clear()
    response = await h.client.get(
        "/platform/v1/auth/callback", params={"code": code, "state": query["state"]}
    )
    assert _error(response) == "state_mismatch"


async def test_a_state_that_is_not_the_cookies_is_a_state_mismatch(build: Builder) -> None:
    h = await build()
    code, _ = await _login(h)
    response = await h.client.get(
        "/platform/v1/auth/callback", params={"code": code, "state": "forged"}
    )
    assert _error(response) == "state_mismatch"


async def test_a_replayed_callback_is_a_state_mismatch(build: Builder) -> None:
    h = await build()
    code, query = await _login(h)
    params = {"code": code, "state": query["state"]}
    first = await h.client.get("/platform/v1/auth/callback", params=params)
    assert first.headers["location"] == "/chat"
    # Replay with the transaction cookie put back, exactly as an attacker holding both would.
    h.client.cookies.set("epicurus_auth_tx", query["state"], path="/platform/v1/auth")
    assert _error(await h.client.get("/platform/v1/auth/callback", params=params)) == (
        "state_mismatch"
    )


async def test_an_expired_login_is_a_state_mismatch(build: Builder) -> None:
    h = await build()
    code, query = await _login(h)
    h.clock.advance(timedelta(minutes=11))
    response = await h.client.get(
        "/platform/v1/auth/callback", params={"code": code, "state": query["state"]}
    )
    assert _error(response) == "state_mismatch"


async def test_a_refused_code_is_a_failed_token_exchange(build: Builder) -> None:
    provider = FakeProvider()
    provider.token_status = 400
    h = await build(provider)
    assert _error(await _sign_in(h)) == "token_exchange_failed"


async def test_a_token_endpoint_that_does_not_answer_is_unreachable(build: Builder) -> None:
    provider = FakeProvider()
    provider.down.add("/token")
    h = await build(provider)
    assert _error(await _sign_in(h)) == "provider_unreachable"


def _tamper(token: str) -> str:
    head, body, sig = token.split(".")
    flipped = ("A" if sig[5] != "A" else "B").join((sig[:5], sig[6:]))
    return f"{head}.{body}.{flipped}"


def _unsigned(claims: dict[str, Any]) -> str:
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."


def _hmac_with_public_key(claims: dict[str, Any]) -> str:
    return jwt.encode(claims, "a-shared-secret-of-sufficient-length!!", algorithm="HS256")


def _sign_with_an_unadvertised_alg(provider: FakeProvider) -> None:
    provider.discovery["id_token_signing_alg_values_supported"] = ["RS256"]
    provider.sign_key, provider.sign_kid, provider.sign_alg = EC_KEY, "ec-1", "ES256"


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(
            lambda p: setattr(
                p,
                "forge",
                lambda c: _tamper(
                    jwt.encode(c, RSA_KEY, algorithm="RS256", headers={"kid": "rsa-1"})
                ),
            ),
            id="tampered-signature",
        ),
        pytest.param(lambda p: setattr(p, "sign_key", STRANGER_KEY), id="wrong-key-same-kid"),
        pytest.param(lambda p: setattr(p, "forge", _unsigned), id="alg-none"),
        pytest.param(lambda p: setattr(p, "forge", _hmac_with_public_key), id="alg-hs256"),
        pytest.param(lambda p: p.overrides.update(iss="https://evil.example.test"), id="wrong-iss"),
        pytest.param(lambda p: p.overrides.update(aud="someone-else"), id="wrong-aud"),
        pytest.param(lambda p: p.overrides.update(aud=[CLIENT_ID, "other"]), id="multi-aud-no-azp"),
        pytest.param(lambda p: p.overrides.update(azp="other"), id="wrong-azp"),
        pytest.param(lambda p: p.overrides.update(exp=int(time.time()) - 120), id="expired"),
        pytest.param(lambda p: p.overrides.update(iat=int(time.time()) + 600), id="iat-future"),
        pytest.param(lambda p: p.overrides.update(nbf=int(time.time()) + 600), id="nbf-future"),
        pytest.param(lambda p: p.overrides.update(nonce="not-this-login"), id="nonce-mismatch"),
        pytest.param(lambda p: p.overrides.update(nonce=None), id="nonce-missing"),
        pytest.param(lambda p: p.overrides.update(sub=None), id="no-subject"),
        pytest.param(lambda p: p.overrides.update(exp=None), id="no-exp"),
        pytest.param(_sign_with_an_unadvertised_alg, id="alg-not-advertised"),
    ],
)
async def test_an_id_token_that_fails_any_check_is_invalid(
    build: Builder, setup: Callable[[FakeProvider], object]
) -> None:
    provider = FakeProvider()
    setup(provider)
    h = await build(provider)
    response = await _sign_in(h)
    assert _error(response) == "invalid_token"
    assert _cookie(response, "epicurus_session") is None


async def test_several_audiences_with_this_client_as_azp_are_accepted(build: Builder) -> None:
    provider = FakeProvider()
    provider.overrides.update(aud=[CLIENT_ID, "other"], azp=CLIENT_ID)
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"


async def test_expiry_within_the_minute_of_leeway_is_accepted(build: Builder) -> None:
    provider = FakeProvider()
    provider.overrides.update(exp=int(time.time()) - 30)
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"


async def test_the_callbacks_iss_parameter_must_be_the_issuer(build: Builder) -> None:
    h = await build()
    code, query = await _login(h)
    params = {"code": code, "state": query["state"], "iss": "https://evil.example.test"}
    assert _error(await h.client.get("/platform/v1/auth/callback", params=params)) == (
        "invalid_token"
    )
    code, query = await _login(h)
    params = {"code": code, "state": query["state"], "iss": ISSUER}
    assert (await h.client.get("/platform/v1/auth/callback", params=params)).headers[
        "location"
    ] == "/chat"


async def test_a_provider_promising_iss_must_send_it(build: Builder) -> None:
    provider = FakeProvider()
    provider.discovery["authorization_response_iss_parameter_supported"] = True
    h = await build(provider)
    assert _error(await _sign_in(h)) == "invalid_token"


async def test_an_unlisted_identity_is_not_allowed_and_logged_by_name(build: Builder) -> None:
    provider = FakeProvider()
    provider.claims["email"] = "stranger@example.com"
    h = await build(provider)
    before = _count("not_allowed")
    response = await _sign_in(h)
    assert _error(response) == "not_allowed"
    assert _cookie(response, "epicurus_session") is None
    assert _count("not_allowed") == before + 1


async def test_an_unverified_allowlisted_email_is_refused(build: Builder) -> None:
    provider = FakeProvider()
    provider.claims["email_verified"] = False
    h = await build(provider)
    assert _error(await _sign_in(h)) == "email_unverified"


async def test_a_groups_rule_with_no_groups_claim_says_so(build: Builder) -> None:
    provider = FakeProvider()
    provider.claims["email"] = "someone@example.com"
    h = await build(provider, oidc_allowed_groups="family")
    assert _error(await _sign_in(h)) == "groups_claim_missing"


# ── userinfo ──────────────────────────────────────────────────────────────────


async def test_userinfo_groups_admit_and_are_stored(build: Builder) -> None:
    provider = FakeProvider()
    provider.claims["email"] = "someone@example.com"
    provider.userinfo = {"sub": "user-1", "groups": ["family"], "name": "From Userinfo"}
    h = await build(provider, oidc_allowed_emails="", oidc_allowed_groups="family")
    assert (await _sign_in(h)).headers["location"] == "/chat"
    user = (await h.client.get("/platform/v1/auth/session")).json()["user"]
    assert user["groups"] == ["family"] and user["name"] == "From Userinfo"
    userinfo_call = next(r for r in provider.requests if r.url.path == "/userinfo")
    assert userinfo_call.headers["authorization"] == "Bearer at"


async def test_userinfo_for_another_subject_is_ignored(build: Builder) -> None:
    provider = FakeProvider()
    provider.claims["email"] = "someone@example.com"
    provider.userinfo = {"sub": "someone-else", "groups": ["family"]}
    h = await build(provider, oidc_allowed_emails="", oidc_allowed_groups="family")
    assert _error(await _sign_in(h)) == "groups_claim_missing"


async def test_userinfo_cannot_re_point_the_subject(build: Builder) -> None:
    provider = FakeProvider()
    provider.userinfo = {
        "sub": "user-1",
        "iss": "https://evil.example.test",
        "email_verified": True,
    }
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"
    async with h.engine.connect() as conn:
        issuer = (await conn.execute(select(_AuthSessionRow.issuer))).scalar_one()
    assert issuer == ISSUER


async def test_a_failing_userinfo_endpoint_does_not_fail_the_sign_in(build: Builder) -> None:
    provider = FakeProvider()
    provider.userinfo = {"sub": "user-1"}
    provider.userinfo_status = 500
    h = await build(provider)
    assert (await _sign_in(h)).headers["location"] == "/chat"
    provider.down.add("/userinfo")
    assert (await _sign_in(h)).headers["location"] == "/chat"


# ── next ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "next_",
    ["//evil.example/", "https://evil.example/", "/\\evil.example", "/platform/v1/auth/logout"],
)
async def test_an_open_redirect_attempt_lands_at_the_root(build: Builder, next_: str) -> None:
    h = await build()
    assert (await _sign_in(h, next_)).headers["location"] == "/"


# ── sessions ──────────────────────────────────────────────────────────────────


async def test_a_session_slides_at_most_once_an_hour(build: Builder) -> None:
    h = await build()
    await _sign_in(h)
    h.clock.advance(timedelta(minutes=30))
    quiet = await h.client.get("/platform/v1/things")
    assert quiet.status_code == 200 and _cookie(quiet, "epicurus_session") is None

    h.clock.advance(timedelta(minutes=31))
    renewed = await h.client.get("/platform/v1/things")
    cookie = _cookie(renewed, "epicurus_session")
    assert renewed.status_code == 200 and cookie is not None
    assert f"Max-Age={30 * 86400}" in cookie
    body = (await h.client.get("/platform/v1/auth/session")).json()
    assert datetime.fromisoformat(body["expires_at"]) == h.clock.now + timedelta(days=30)
    async with h.engine.connect() as conn:
        stored = (await conn.execute(select(_AuthSessionRow.expires_at))).scalar_one()
    assert stored.replace(tzinfo=UTC) == h.clock.now + timedelta(days=30)


async def test_the_session_endpoint_renews_too(build: Builder) -> None:
    h = await build()
    await _sign_in(h)
    h.clock.advance(timedelta(hours=2))
    response = await h.client.get("/platform/v1/auth/session")
    assert response.json()["signed_in"] is True
    assert _cookie(response, "epicurus_session") is not None


async def test_an_expired_session_never_authenticates(build: Builder) -> None:
    h = await build(auth_session_days=1)
    await _sign_in(h)
    h.clock.advance(timedelta(days=1, seconds=1))
    assert (await h.client.get("/platform/v1/things")).status_code == 401
    assert (await h.client.get("/platform/v1/auth/session")).json()["signed_in"] is False


async def test_logout_ends_the_session_everywhere_it_was_cached(build: Builder) -> None:
    h = await build()
    await _sign_in(h)
    assert (await h.client.get("/platform/v1/things")).status_code == 200  # now cached
    token = h.client.cookies.get("epicurus_session")
    response = await h.client.post(
        "/platform/v1/auth/logout", headers={"sec-fetch-site": "same-origin"}
    )
    assert response.status_code == 200 and response.json() == {"signed_out": True}
    assert response.headers["cache-control"] == "no-store"
    cleared = _cookie(response, "epicurus_session")
    assert cleared is not None and "Max-Age=0" in cleared
    # Even presenting the old token again (a copied cookie) no longer opens anything.
    assert token is not None
    h.client.cookies.set("epicurus_session", token)
    assert (await h.client.get("/platform/v1/things")).status_code == 401


async def test_logout_without_a_session_still_answers_200(build: Builder) -> None:
    h = await build()
    response = await h.client.post("/platform/v1/auth/logout")
    assert response.status_code == 200 and response.json() == {"signed_out": True}


async def test_a_garbage_session_cookie_is_simply_signed_out(build: Builder) -> None:
    h = await build()
    h.client.cookies.set("epicurus_session", "not-a-token")
    assert (await h.client.get("/platform/v1/things")).status_code == 401
    assert (await h.client.get("/platform/v1/auth/session")).json()["signed_in"] is False


# ── the middleware ────────────────────────────────────────────────────────────


async def test_the_401_is_the_documented_json(build: Builder) -> None:
    h = await build()
    response = await h.client.get("/platform/v1/things")
    assert response.status_code == 401
    assert response.json() == {"detail": "Sign in to continue.", "code": "unauthenticated"}
    assert response.headers["cache-control"] == "no-store"
    # Compact like every FastAPI response — the smoke gates grep for this exact byte form.
    assert response.content == b'{"detail":"Sign in to continue.","code":"unauthenticated"}'


@pytest.mark.parametrize(
    "header",
    [
        {"x-forwarded-for": "1.2.3.4"},
        {"forwarded": "for=1.2.3.4"},
        {"x-forwarded-host": "epicurus.example.com"},
        {"x-forwarded-proto": "https"},
        {"x-real-ip": "1.2.3.4"},
    ],
)
async def test_any_proxy_header_makes_a_request_proxied(
    build: Builder, header: dict[str, str]
) -> None:
    h = await build()
    assert (await h.direct.get("/platform/v1/things", headers=header)).status_code == 401


async def test_a_direct_request_passes_untouched_in_oidc_mode(build: Builder) -> None:
    h = await build()
    response = await h.direct.get("/platform/v1/things")
    assert response.status_code == 200 and response.json() == {"subject": None}
    cross = await h.direct.post("/platform/v1/things", headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 200  # the cross-site check is for the proxied door only


async def test_health_and_the_auth_endpoints_are_exempt(build: Builder) -> None:
    h = await build()
    assert (await h.client.get("/health")).status_code == 200
    assert (await h.client.get("/platform/v1/auth/session")).status_code == 200


def test_the_exemption_is_not_fooled_by_dot_segments() -> None:
    assert _exempt("/health") and _exempt("/platform/v1/auth/session")
    assert not _exempt("/platform/v1/auth/../things")
    assert not _exempt("/platform/v1/authx")
    assert not _exempt("/health/x")


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"sec-fetch-site": "same-origin"}, 200),
        ({"sec-fetch-site": "none"}, 200),
        ({"sec-fetch-site": "same-site"}, 403),
        ({"sec-fetch-site": "cross-site"}, 403),
        ({"sec-fetch-site": "Same-Origin", "origin": "https://evil.example"}, 200),
        ({"origin": PUBLIC}, 200),
        ({"origin": "http://localhost:8084/"}, 200),
        ({"origin": "http://sibling.localhost:8084"}, 403),
        ({"origin": "null"}, 403),
        ({}, 200),
    ],
)
async def test_the_cross_site_check_on_unsafe_methods(
    build: Builder, headers: dict[str, str], status: int
) -> None:
    h = await build()
    await _sign_in(h)
    response = await h.client.post("/platform/v1/things", headers=headers)
    assert response.status_code == status
    if status == 403:
        assert response.json() == {"detail": "Cross-site request refused.", "code": "cross_site"}
        assert response.headers["cache-control"] == "no-store"


async def test_a_cross_site_logout_is_refused(build: Builder) -> None:
    h = await build()
    await _sign_in(h)
    response = await h.client.post(
        "/platform/v1/auth/logout", headers={"sec-fetch-site": "cross-site"}
    )
    assert response.status_code == 403
    assert (await h.client.get("/platform/v1/things")).status_code == 200


async def test_safe_methods_are_not_cross_site_checked(build: Builder) -> None:
    h = await build()
    await _sign_in(h)
    response = await h.client.get("/platform/v1/things", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 200


async def test_sse_streams_through_the_middleware(build: Builder) -> None:
    h = await build()
    await _sign_in(h)
    async with h.client.stream("GET", "/platform/v1/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b"".join([chunk async for chunk in response.aiter_bytes()])
    assert body == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


async def test_the_middleware_never_buffers_a_response_body(build: Builder) -> None:
    """Each body chunk reaches the server before the app has produced the next one."""
    h = await build()
    await _sign_in(h)
    h.clock.advance(timedelta(hours=2))  # the renewal path wraps `send` — the harder case
    token = h.client.cookies.get("epicurus_session")
    release = asyncio.Event()
    delivered: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await release.wait()
        await send({"type": "http.response.body", "body": b"second"})

    middleware = AuthMiddleware(app, service=h.service)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/platform/v1/stream",
        "headers": [
            (b"x-forwarded-for", b"1.2.3.4"),
            (b"cookie", f"epicurus_session={token}".encode()),
        ],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: Any) -> None:
        delivered.append(message)

    task = asyncio.create_task(middleware(scope, receive, send))
    for _ in range(100):
        if len(delivered) == 2:
            break
        await asyncio.sleep(0.01)
    assert [m.get("body") for m in delivered] == [None, b"first"]
    assert any(k == b"set-cookie" for k, _ in delivered[0]["headers"])
    release.set()
    await task
    assert delivered[-1]["body"] == b"second"


async def test_a_proxied_websocket_without_a_session_is_refused(build: Builder) -> None:
    h = await build()
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        raise AssertionError("the app must not see an unauthenticated proxied websocket")

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    scope = {"type": "websocket", "path": "/ws", "headers": [(b"x-real-ip", b"1.2.3.4")]}
    await AuthMiddleware(app, service=h.service)(scope, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


# ── AUTH_MODE=none ────────────────────────────────────────────────────────────


async def test_mode_none_is_a_strict_no_op(build: Builder) -> None:
    h = await build(auth_mode="none")
    seen: list[tuple[Any, Any, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append((scope, receive, send))

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: Any) -> None:
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/platform/v1/things",
        "headers": [(b"x-forwarded-for", b"1.2.3.4"), (b"sec-fetch-site", b"cross-site")],
    }
    await AuthMiddleware(app, service=h.service)(scope, receive, send)
    assert seen == [(scope, receive, send)]
    assert seen[0][0] is scope and seen[0][1] is receive and seen[0][2] is send
    assert "state" not in scope

    # And through the app: nothing refused, nothing added.
    assert (await h.client.get("/platform/v1/things")).status_code == 200
    cross = await h.client.post("/platform/v1/things", headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 200


async def test_mode_none_endpoints(build: Builder) -> None:
    # The oidc presentation knobs set beside AUTH_MODE=none stay inert: the documented contract
    # is `provider_name: null` and `auto_redirect: false` whenever sign-in is off.
    h = await build(auth_mode="none", oidc_provider_name="Pocket ID", oidc_auto_redirect=True)
    session = await h.client.get("/platform/v1/auth/session")
    assert session.json() == {
        "mode": "none",
        "signed_in": False,
        "provider_name": None,
        "auto_redirect": False,
        "user": None,
        "expires_at": None,
    }
    disabled = {"detail": "Sign-in is not enabled on this deployment.", "code": "auth_disabled"}
    for path in ("/platform/v1/auth/login", "/platform/v1/auth/callback"):
        response = await h.client.get(path)
        assert response.status_code == 404 and response.json() == disabled
        assert response.headers["cache-control"] == "no-store"
    logout = await h.client.post("/platform/v1/auth/logout")
    assert logout.status_code == 200 and logout.json() == {"signed_out": True}
    assert h.provider.requests == []


# ── the real app ──────────────────────────────────────────────────────────────


def _oidc_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    database = f"sqlite+aiosqlite:///{tmp_path / 'core.db'}"
    for name, value in {
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER_URL": ISSUER,
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_ALLOWED_EMAILS": "me@example.com",
        "OAUTH_REDIRECT_BASE_URL": PUBLIC,
        "DATABASE_URL": database,
    }.items():
        monkeypatch.setenv(name, value)
    return database


async def test_the_real_app_signs_in_and_guards_the_platform_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import epicurus_core_app.app as app_module

    database = _oidc_env(monkeypatch, tmp_path)
    provider = FakeProvider()
    monkeypatch.setattr(
        app_module, "build_auth", functools.partial(build_auth, transport=provider.transport)
    )
    app = app_module.create_app()
    # No lifespan here (it would connect NATS), so the two tables come from the store.
    engine = create_async_engine(database)
    await AuthStore(engine).init()
    transport = httpx.ASGITransport(app=app)
    browser = httpx.AsyncClient(transport=transport, base_url=PUBLIC, headers=XFF)
    module = httpx.AsyncClient(transport=transport, base_url="http://core-app:8080")
    try:
        assert (await browser.get("/platform/v1/info")).status_code == 401
        assert (await browser.get("/health")).status_code == 200
        assert (await module.get("/platform/v1/info")).status_code == 200

        login = await browser.get("/platform/v1/auth/login", params={"next": "/settings"})
        code, query = provider.authorize(login.headers["location"])
        callback = await browser.get(
            "/platform/v1/auth/callback", params={"code": code, "state": query["state"]}
        )
        assert callback.headers["location"] == "/settings"
        info = await browser.get("/platform/v1/info")
        assert info.status_code == 200 and info.json()["tenant"] == "local"
        assert (await browser.get("/platform/v1/auth/session")).json()["signed_in"] is True
    finally:
        await browser.aclose()
        await module.aclose()
        await engine.dispose()


async def test_the_real_app_in_mode_none_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from epicurus_core_app.app import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'core.db'}")
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url=PUBLIC, headers=XFF) as browser:
        assert (await browser.get("/platform/v1/info")).status_code == 200
        body = (await browser.get("/platform/v1/auth/session")).json()
        assert body["mode"] == "none" and body["signed_in"] is False


def test_the_real_app_refuses_to_start_half_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from epicurus_core_app.app import create_app

    _oidc_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OIDC_CLIENT_ID", "")
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "")
    with pytest.raises(AuthConfigError) as err:
        create_app()
    assert "OIDC_CLIENT_ID is not set" in str(err.value)
    assert "no admission rule" in str(err.value)
