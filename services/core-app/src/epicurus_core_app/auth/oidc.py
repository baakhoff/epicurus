"""The OpenID Connect relying party: discovery, JWKS, the code exchange, ID-token validation.

The core signs a user in with the **authorization code flow + PKCE** (S256, always, public
client or not), a ``state`` bound to an HttpOnly transaction cookie, and a ``nonce`` bound to
the ID token. Everything the provider says is fetched here, over async ``httpx`` — never
``PyJWKClient``, whose synchronous ``urllib`` fetch would block the event loop the whole core
shares — and every answer is checked before it is believed:

* **Discovery** (``<issuer>/.well-known/openid-configuration``) must name the configured issuer
  (modulo one trailing slash), advertise a code flow, and sign ID tokens with at least one
  asymmetric algorithm this client accepts. Cached for an hour; a failure is never cached, and a
  provider that stops answering mid-flow invalidates it, so the next sign-in re-discovers.
* **ID tokens** are verified against the provider's JWKS with the algorithm pinned to
  :data:`ALLOWED_SIGNING_ALGS` ∩ what discovery advertises — never ``none``, never an HMAC
  algorithm (whose "key" would be a public key an attacker can read). ``iss`` must be the
  discovery issuer, ``aud`` must contain the client id (``azp`` must be it when set, and must be
  set when there are several audiences), ``exp``/``iat``/``nbf`` are checked with a minute of
  leeway, and the ``nonce`` must be the one this login stored.
* **JWKS** is cached too. A token naming a ``kid`` the cache has never seen triggers one
  refetch — providers rotate keys — rate-limited so a stream of garbage tokens cannot turn the
  core into a JWKS fetch amplifier.
* **Userinfo**, when the provider has an endpoint, fills in the profile claims an ID token often
  omits (Pocket ID puts ``groups`` there). Its ``sub`` must match the ID token's or none of it
  is used; its failure is a WARNING, never a failed sign-in.

Nothing here logs a code, a token, a nonce, a verifier or the client secret.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt

from epicurus_core import get_logger
from epicurus_core_app.auth.config import AuthConfig
from epicurus_core_app.auth.errors import AuthFlowError

__all__ = [
    "ALLOWED_SIGNING_ALGS",
    "OidcClient",
    "ProviderMetadata",
    "TokenSet",
    "code_challenge_for",
    "merge_claims",
    "new_code_verifier",
]

log = get_logger("epicurus_core_app.auth")

#: The only ID-token signature algorithms this client will verify — asymmetric, all of them.
ALLOWED_SIGNING_ALGS: Final[tuple[str, ...]] = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "ES256",
    "ES384",
    "EdDSA",
)

DISCOVERY_TTL_S: Final = 3600.0
JWKS_TTL_S: Final = 3600.0
#: The floor between two JWKS fetches forced by an unknown ``kid``.
JWKS_REFETCH_MIN_INTERVAL_S: Final = 30.0
HTTP_TIMEOUT_S: Final = 10.0
#: Leeway for ``exp`` / ``iat`` / ``nbf`` — clocks on a home lab drift.
CLOCK_SKEW_S: Final = 60

# kty (and the curves) a JWK must have to verify each algorithm.
_KEY_SHAPES: Final[dict[str, tuple[str, frozenset[str] | None]]] = {
    "RS256": ("RSA", None),
    "RS384": ("RSA", None),
    "RS512": ("RSA", None),
    "PS256": ("RSA", None),
    "ES256": ("EC", frozenset({"P-256"})),
    "ES384": ("EC", frozenset({"P-384"})),
    "EdDSA": ("OKP", frozenset({"Ed25519", "Ed448"})),
}

# Claims that describe the token rather than the person: the ID token's values always win a
# merge, so a userinfo response can never re-point `sub` or `iss`, or restate an audience.
_PROTOCOL_CLAIMS: Final = frozenset(
    {
        "iss",
        "sub",
        "aud",
        "exp",
        "iat",
        "nbf",
        "nonce",
        "azp",
        "auth_time",
        "at_hash",
        "c_hash",
        "acr",
        "amr",
        "sid",
        "jti",
    }
)


@dataclass(frozen=True)
class ProviderMetadata:
    """The parts of a discovery document this client uses, checked."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    #: :data:`ALLOWED_SIGNING_ALGS` ∩ ``id_token_signing_alg_values_supported``, in that order.
    signing_algs: tuple[str, ...]
    #: ``token_endpoint_auth_methods_supported``, or ``None`` when the provider does not say.
    token_auth_methods: tuple[str, ...] | None
    #: RFC 9207: the provider promises an ``iss`` parameter on every authorization response.
    iss_parameter_supported: bool


@dataclass(frozen=True)
class TokenSet:
    """What the token endpoint returned that the flow needs. ``repr`` hides both tokens."""

    id_token: str
    access_token: str | None

    def __repr__(self) -> str:
        return "TokenSet(<redacted>)"


def new_code_verifier() -> str:
    """A PKCE code verifier: 86 characters from the unreserved set (RFC 7636 §4.1)."""
    return secrets.token_urlsafe(64)


def code_challenge_for(verifier: str) -> str:
    """The S256 challenge for *verifier*: ``BASE64URL(SHA256(verifier))``, unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def merge_claims(id_claims: Mapping[str, Any], userinfo: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ID token's claims with userinfo's profile claims layered over them.

    Userinfo is usually the fuller, fresher picture of the person (``groups`` often lives only
    there), so it wins for *profile* claims. It never wins for a protocol claim — ``sub``,
    ``iss`` and the rest of :data:`_PROTOCOL_CLAIMS` are the verified ID token's.
    """
    merged = dict(id_claims)
    for name, value in (userinfo or {}).items():
        if name not in _PROTOCOL_CLAIMS:
            merged[name] = value
    return merged


def _strip_one_slash(value: str) -> str:
    return value[:-1] if value.endswith("/") else value


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _describe(exc: BaseException) -> str:
    """An exception as a log-safe phrase: its type and message, never a token's contents."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def parse_metadata(doc: Mapping[str, Any], *, configured_issuer: str) -> ProviderMetadata:
    """Check a discovery document against this client; raise ``misconfigured`` if it won't do."""
    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or _strip_one_slash(issuer) != _strip_one_slash(
        configured_issuer
    ):
        raise AuthFlowError(
            "misconfigured",
            f"the provider's discovery document names issuer {issuer!r}, but "
            f"OIDC_ISSUER_URL is {configured_issuer!r}",
        )
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not _is_http_url(doc.get(key)):
            raise AuthFlowError(
                "misconfigured", f"the provider's discovery document has no usable {key}"
            )
    response_types = doc.get("response_types_supported")
    if isinstance(response_types, list) and "code" not in response_types:
        raise AuthFlowError(
            "misconfigured", "the provider does not support the authorization code flow"
        )
    challenge_methods = doc.get("code_challenge_methods_supported")
    if isinstance(challenge_methods, list) and "S256" not in challenge_methods:
        raise AuthFlowError("misconfigured", "the provider does not support PKCE with S256")
    advertised = doc.get("id_token_signing_alg_values_supported")
    if not isinstance(advertised, list):
        advertised = ["RS256"]  # the OpenID Connect default, and the one every provider has
    algs = tuple(alg for alg in ALLOWED_SIGNING_ALGS if alg in advertised)
    if not algs:
        raise AuthFlowError(
            "misconfigured",
            f"the provider signs ID tokens with {advertised}; this client accepts only "
            f"{list(ALLOWED_SIGNING_ALGS)}",
        )
    methods = doc.get("token_endpoint_auth_methods_supported")
    userinfo = doc.get("userinfo_endpoint")
    return ProviderMetadata(
        issuer=issuer,
        authorization_endpoint=str(doc["authorization_endpoint"]),
        token_endpoint=str(doc["token_endpoint"]),
        jwks_uri=str(doc["jwks_uri"]),
        userinfo_endpoint=userinfo if _is_http_url(userinfo) else None,
        signing_algs=algs,
        token_auth_methods=(
            tuple(m for m in methods if isinstance(m, str)) if isinstance(methods, list) else None
        ),
        iss_parameter_supported=doc.get("authorization_response_iss_parameter_supported") is True,
    )


def _candidate_keys(
    keys: list[dict[str, Any]], *, alg: str, kid: str | None
) -> list[dict[str, Any]]:
    """The JWKs that could have signed a token with this ``alg`` (and ``kid``, if it names one)."""
    kty, curves = _KEY_SHAPES[alg]
    return [
        jwk
        for jwk in keys
        if jwk.get("kty") == kty
        and (curves is None or jwk.get("crv") in curves)
        and jwk.get("use") in (None, "sig")
        and jwk.get("alg") in (None, alg)
        and (kid is None or jwk.get("kid") == kid)
    ]


class OidcClient:
    """One provider, as this deployment is configured to use it.

    Holds only caches — the discovery document and the JWKS — and no per-user state; the flow's
    state lives in the login-state rows (:mod:`.store`), so a restart mid-login loses nothing.
    """

    def __init__(
        self,
        config: AuthConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        # Injected by tests (an httpx.MockTransport standing in for the provider); a real
        # network transport — TLS verification on, the platform's trust store — when None.
        self._transport = transport
        self._monotonic = monotonic
        self._metadata: ProviderMetadata | None = None
        self._metadata_at = 0.0
        self._metadata_lock = asyncio.Lock()
        self._jwks: list[dict[str, Any]] | None = None
        self._jwks_uri: str | None = None
        self._jwks_at = 0.0
        self._jwks_lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_S,
            transport=self._transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    # ── discovery ────────────────────────────────────────────────────────────────

    async def metadata(self) -> ProviderMetadata:
        """The provider's checked discovery document — cached for an hour, fetched lazily."""
        cached = self._metadata
        if cached is not None and self._monotonic() - self._metadata_at < DISCOVERY_TTL_S:
            return cached
        async with self._metadata_lock:
            cached = self._metadata
            if cached is not None and self._monotonic() - self._metadata_at < DISCOVERY_TTL_S:
                return cached
            fresh = await self._fetch_metadata()
            self._metadata, self._metadata_at = fresh, self._monotonic()
            return fresh

    def invalidate(self) -> None:
        """Forget the cached discovery document, so the next sign-in fetches it again."""
        self._metadata = None

    async def _fetch_metadata(self) -> ProviderMetadata:
        url = f"{self._config.issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            async with self._client() as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise AuthFlowError(
                "provider_unreachable", f"OIDC discovery at {url} failed: {_describe(exc)}"
            ) from exc
        if response.status_code >= 500:
            raise AuthFlowError(
                "provider_unreachable", f"OIDC discovery at {url} answered {response.status_code}"
            )
        if response.status_code != 200:
            raise AuthFlowError(
                "misconfigured",
                f"OIDC discovery at {url} answered {response.status_code} — is "
                "OIDC_ISSUER_URL the provider's issuer?",
            )
        doc = _json_object(response)
        if doc is None:
            raise AuthFlowError("misconfigured", f"OIDC discovery at {url} is not a JSON object")
        return parse_metadata(doc, configured_issuer=self._config.issuer)

    # ── the authorization request ────────────────────────────────────────────────

    def authorization_url(
        self, meta: ProviderMetadata, *, state: str, nonce: str, code_challenge: str
    ) -> str:
        """Where to send the browser: the provider's authorization endpoint, with the request."""
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": " ".join(self._config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        separator = "&" if urlsplit(meta.authorization_endpoint).query else "?"
        return f"{meta.authorization_endpoint}{separator}{urlencode(params)}"

    # ── the code exchange ────────────────────────────────────────────────────────

    async def exchange_code(
        self, meta: ProviderMetadata, *, code: str, code_verifier: str
    ) -> TokenSet:
        """Trade the authorization code (and the PKCE verifier) for tokens.

        A confidential client authenticates with ``client_secret_basic`` unless the provider
        advertises only ``client_secret_post``; a public client sends its ``client_id`` in the
        body and nothing else — PKCE is what binds the code to this login.
        """
        config = self._config
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
            "code_verifier": code_verifier,
        }
        auth: httpx.BasicAuth | None = None
        if config.client_secret:
            methods = meta.token_auth_methods
            if (
                methods is not None
                and "client_secret_basic" not in methods
                and "client_secret_post" in methods
            ):
                form["client_id"] = config.client_id
                form["client_secret"] = config.client_secret
            else:
                # RFC 6749 §2.3.1: each half is form-urlencoded before it is base64'd.
                auth = httpx.BasicAuth(
                    quote(config.client_id, safe=""), quote(config.client_secret, safe="")
                )
        else:
            form["client_id"] = config.client_id
        try:
            async with self._client() as client:
                response = await client.post(meta.token_endpoint, data=form, auth=auth)
        except httpx.HTTPError as exc:
            self.invalidate()
            raise AuthFlowError(
                "provider_unreachable", f"the token endpoint could not be reached: {_describe(exc)}"
            ) from exc
        body = _json_object(response)
        if response.status_code != 200:
            error = body.get("error") if body else None
            raise AuthFlowError(
                "token_exchange_failed",
                f"the token endpoint answered {response.status_code}"
                + (f" ({error})" if isinstance(error, str) else ""),
            )
        if body is None:
            raise AuthFlowError("token_exchange_failed", "the token response is not a JSON object")
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise AuthFlowError(
                "token_exchange_failed",
                "the token response carries no id_token — is `openid` among the client's scopes?",
            )
        access_token = body.get("access_token")
        return TokenSet(
            id_token=id_token,
            access_token=access_token if isinstance(access_token, str) and access_token else None,
        )

    # ── the ID token ─────────────────────────────────────────────────────────────

    async def _signing_keys(self, meta: ProviderMetadata, *, refresh: bool) -> list[dict[str, Any]]:
        """The provider's JWKS — cached; *refresh* forces a refetch at most every 30 seconds."""
        async with self._jwks_lock:
            now = self._monotonic()
            cached = self._jwks if self._jwks_uri == meta.jwks_uri else None
            if cached is not None:
                age = now - self._jwks_at
                if not refresh and age < JWKS_TTL_S:
                    return cached
                if refresh and age < JWKS_REFETCH_MIN_INTERVAL_S:
                    return cached
            keys = await self._fetch_jwks(meta)
            self._jwks, self._jwks_uri, self._jwks_at = keys, meta.jwks_uri, self._monotonic()
            return keys

    async def _fetch_jwks(self, meta: ProviderMetadata) -> list[dict[str, Any]]:
        try:
            async with self._client() as client:
                response = await client.get(meta.jwks_uri)
        except httpx.HTTPError as exc:
            self.invalidate()
            raise AuthFlowError(
                "provider_unreachable", f"the provider's JWKS could not be fetched: {_describe(exc)}"
            ) from exc
        if response.status_code >= 500:
            self.invalidate()
            raise AuthFlowError(
                "provider_unreachable", f"the provider's JWKS answered {response.status_code}"
            )
        body = _json_object(response) if response.status_code == 200 else None
        keys = body.get("keys") if body else None
        if not isinstance(keys, list):
            raise AuthFlowError("misconfigured", "the provider's JWKS is not a JSON key set")
        return [key for key in keys if isinstance(key, dict)]

    async def validate_id_token(
        self, meta: ProviderMetadata, id_token: str, *, nonce: str
    ) -> dict[str, Any]:
        """Verify *id_token* and return its claims, or raise ``invalid_token`` saying why."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise AuthFlowError(
                "invalid_token", f"the ID token is not a well-formed JWT ({_describe(exc)})"
            ) from exc
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in meta.signing_algs:
            raise AuthFlowError(
                "invalid_token",
                f"the ID token is signed with {alg!r}; this client accepts "
                f"{list(meta.signing_algs)}",
            )
        kid = header.get("kid") if isinstance(header.get("kid"), str) else None
        candidates = _candidate_keys(
            await self._signing_keys(meta, refresh=False), alg=alg, kid=kid
        )
        if not candidates:
            # A key this cache has never seen: providers rotate, so look once more.
            candidates = _candidate_keys(
                await self._signing_keys(meta, refresh=True), alg=alg, kid=kid
            )
        if not candidates:
            raise AuthFlowError(
                "invalid_token", f"no key in the provider's JWKS can verify this ID token ({alg})"
            )
        claims = self._verify(id_token, candidates, alg=alg, issuer=meta.issuer)
        self._check_claims(claims, nonce=nonce)
        return claims

    def _verify(
        self, id_token: str, candidates: list[dict[str, Any]], *, alg: str, issuer: str
    ) -> dict[str, Any]:
        for jwk in candidates:
            try:
                key = jwt.PyJWK(jwk, algorithm=alg)
            except (jwt.PyJWTError, ValueError, TypeError):
                continue  # a key this library cannot load cannot have signed anything we accept
            try:
                return jwt.decode(
                    id_token,
                    key=key.key,
                    algorithms=[alg],
                    audience=self._config.client_id,
                    issuer=issuer,
                    leeway=CLOCK_SKEW_S,
                    options={"require": ["iss", "sub", "aud", "exp", "iat"]},
                )
            except jwt.InvalidSignatureError:
                continue  # another candidate may be the signer (a JWKS with no kids)
            except (jwt.PyJWTError, ValueError, TypeError) as exc:
                raise AuthFlowError(
                    "invalid_token", f"the ID token was rejected: {_describe(exc)}"
                ) from exc
        raise AuthFlowError("invalid_token", "the ID token's signature does not verify")

    def _check_claims(self, claims: Mapping[str, Any], *, nonce: str) -> None:
        client_id = self._config.client_id
        audience = claims.get("aud")
        azp = claims.get("azp")
        if isinstance(audience, list) and len(audience) > 1 and azp is None:
            raise AuthFlowError(
                "invalid_token", "the ID token has several audiences and no authorized party"
            )
        if azp is not None and azp != client_id:
            raise AuthFlowError(
                "invalid_token", "the ID token's authorized party (azp) is not this client"
            )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthFlowError("invalid_token", "the ID token has no subject")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce.encode(), nonce.encode()
        ):
            raise AuthFlowError("invalid_token", "the ID token's nonce is not this login's")

    # ── userinfo ─────────────────────────────────────────────────────────────────

    async def userinfo(
        self, meta: ProviderMetadata, access_token: str | None, *, subject: str
    ) -> dict[str, Any] | None:
        """The userinfo claims for *subject*, or ``None`` — never a failed sign-in.

        A provider that does not answer, answers with something unreadable, or answers for a
        different ``sub`` costs the profile claims userinfo would have added, logged at
        WARNING; the ID token's own claims still decide the sign-in.
        """
        if meta.userinfo_endpoint is None or not access_token:
            return None
        try:
            async with self._client() as client:
                response = await client.get(
                    meta.userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"}
                )
        except httpx.HTTPError as exc:
            log.warning("userinfo unavailable; using the ID token's claims", error=_describe(exc))
            return None
        if response.status_code != 200:
            log.warning(
                "userinfo unavailable; using the ID token's claims", status=response.status_code
            )
            return None
        if response.headers.get("content-type", "").split(";")[0].strip() == "application/jwt":
            log.warning("signed userinfo responses are not supported; using the ID token's claims")
            return None
        body = _json_object(response)
        if body is None:
            log.warning("userinfo is not a JSON object; using the ID token's claims")
            return None
        if body.get("sub") != subject:
            log.warning("userinfo answered for a different subject; ignoring it", subject=subject)
            return None
        return body

    # ── the whole callback leg ───────────────────────────────────────────────────

    async def authenticate(
        self, meta: ProviderMetadata, *, code: str, code_verifier: str, nonce: str
    ) -> dict[str, Any]:
        """Exchange, validate and enrich: the verified claims of the person signing in."""
        tokens = await self.exchange_code(meta, code=code, code_verifier=code_verifier)
        claims = await self.validate_id_token(meta, tokens.id_token, nonce=nonce)
        info = await self.userinfo(meta, tokens.access_token, subject=str(claims["sub"]))
        return merge_claims(claims, info)
