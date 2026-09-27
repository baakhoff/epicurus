"""Sign-in configuration: read once from :class:`CoreAppSettings`, validated fail-closed (#969).

``AUTH_MODE=oidc`` is the operator saying "this deployment has a door". A door that is half
configured is worse than none, so :func:`load_auth_config` refuses to produce a config — and
the core refuses to start — unless it has an issuer, a client id and an **admission rule**. The
last one is the one that matters: an empty allowlist must never mean "everyone", because pointed
at a public provider (Google) that admits the internet. "Everyone the provider authenticates for
this client" is a legitimate choice for a private provider, so it exists — as
``OIDC_ALLOW_ALL_USERS=true``, said out loud.

What is *not* checked here is the provider itself: discovery is fetched lazily on the first
sign-in, so a provider that is down never blocks startup (and a deployment whose provider is
briefly unreachable still serves every signed-in session it already has).

With ``AUTH_MODE=none`` nothing here can fail: that mode is today's behaviour, and a setting
that never mattered before must not start mattering because this module exists.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from epicurus_core import get_logger
from epicurus_core_app.settings import AuthMode, CoreAppSettings

__all__ = [
    "CALLBACK_PATH",
    "AuthConfig",
    "AuthConfigError",
    "is_loopback_host",
    "load_auth_config",
    "origin_of",
    "parse_list",
    "parse_scopes",
]

log = get_logger("epicurus_core_app.auth")

#: Where the provider sends the browser back to — appended to ``OAUTH_REDIRECT_BASE_URL``.
CALLBACK_PATH = "/platform/v1/auth/callback"

_DEFAULT_SCOPES = ("openid", "email", "profile")


class AuthConfigError(RuntimeError):
    """``AUTH_MODE=oidc`` with a configuration the core refuses to run. Fails startup."""


@dataclass(frozen=True)
class AuthConfig:
    """Everything the sign-in flow and the enforcement middleware need, parsed and checked."""

    mode: AuthMode
    #: The tenant every session and login row is scoped to — the default tenant in v1.
    tenant: str
    #: ``scheme://host[:port]`` of the public URL — what a same-origin ``Origin`` header says.
    public_origin: str
    #: Cookies carry ``Secure`` exactly when the public URL is https.
    secure_cookies: bool
    redirect_uri: str
    issuer: str
    client_id: str
    #: Blank for a public client. ``repr=False``: this object is never a way to print it.
    client_secret: str = field(repr=False)
    scopes: tuple[str, ...]
    provider_name: str | None
    allowed_emails: frozenset[str]
    allowed_groups: frozenset[str]
    allow_all_users: bool
    auto_redirect: bool
    session_days: int

    @property
    def enabled(self) -> bool:
        """Whether sign-in is on at all — ``AUTH_MODE=oidc``."""
        return self.mode == "oidc"


def parse_scopes(raw: str) -> tuple[str, ...]:
    """``OIDC_SCOPES``, space- or comma-separated, de-duplicated, ``openid`` always first.

    ``openid`` is what makes the request an OpenID Connect one at all — without it there is no
    ID token — so it is added rather than trusted to be present. Blank means the default.
    """
    scopes = [part for part in re.split(r"[\s,]+", raw) if part]
    if not scopes:
        scopes = list(_DEFAULT_SCOPES)
    ordered = ["openid", *(scope for scope in scopes if scope != "openid")]
    return tuple(dict.fromkeys(ordered))


def parse_list(raw: str, *, lower: bool = False) -> frozenset[str]:
    """A comma-separated allowlist: entries trimmed, blanks dropped, optionally lowercased."""
    items = (item.strip() for item in raw.split(","))
    return frozenset(item.lower() if lower else item for item in items if item)


def is_loopback_host(host: str | None) -> bool:
    """``localhost``, ``*.localhost`` (RFC 6761) or a loopback IP — never a real network hop."""
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_of(url: str) -> str | None:
    """The serialised origin of an absolute http(s) URL, or ``None`` if it is not one.

    Lowercase scheme and host and no default port, so ``https://Example.com:443/x`` and the
    browser's ``Origin: https://example.com`` compare equal.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    if ":" in host:  # an IPv6 literal: urlsplit strips the brackets, the origin keeps them
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{suffix}"


def _scheme(url: str) -> str:
    """The lowercase scheme of *url*, or ``""`` for something that does not parse as one."""
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _url_problem(name: str, value: str) -> str | None:
    """Why *value* is not an absolute http(s) URL with a host, or ``None`` if it is one."""
    if not value:
        return f"{name} is not set"
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 — a malformed port raises only when read
    except ValueError:
        return f"{name} is not a valid URL ({value!r})"
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return f"{name} must be an absolute http(s) URL (got {value!r})"
    if parts.query or parts.fragment:
        return f"{name} must not carry a query string or fragment (got {value!r})"
    return None


def load_auth_config(settings: CoreAppSettings) -> AuthConfig:
    """Parse and check the sign-in settings; raise :class:`AuthConfigError` if oidc can't run.

    Every problem is collected before raising, so an operator fixing a fresh install reads one
    line naming everything that is missing rather than restarting once per key.
    """
    public_url = settings.oauth_redirect_base_url.strip().rstrip("/")
    issuer = settings.oidc_issuer_url.strip()
    client_id = settings.oidc_client_id.strip()
    allowed_emails = parse_list(settings.oidc_allowed_emails, lower=True)
    allowed_groups = parse_list(settings.oidc_allowed_groups)
    config = AuthConfig(
        mode=settings.auth_mode,
        tenant=settings.default_tenant_id,
        public_origin=origin_of(public_url) or "",
        secure_cookies=_scheme(public_url) == "https",
        redirect_uri=f"{public_url}{CALLBACK_PATH}",
        issuer=issuer,
        client_id=client_id,
        client_secret=settings.oidc_client_secret.get_secret_value().strip(),
        scopes=parse_scopes(settings.oidc_scopes),
        provider_name=settings.oidc_provider_name.strip() or None,
        allowed_emails=allowed_emails,
        allowed_groups=allowed_groups,
        allow_all_users=settings.oidc_allow_all_users,
        auto_redirect=settings.oidc_auto_redirect,
        session_days=settings.auth_session_days,
    )
    if not config.enabled:
        return config

    problems: list[str] = []
    for name, value in (("OIDC_ISSUER_URL", issuer), ("OAUTH_REDIRECT_BASE_URL", public_url)):
        problem = _url_problem(name, value)
        if problem is not None:
            problems.append(problem)
    if not client_id:
        problems.append("OIDC_CLIENT_ID is not set")
    if not (allowed_emails or allowed_groups or config.allow_all_users):
        problems.append(
            "no admission rule is set — set OIDC_ALLOWED_EMAILS, OIDC_ALLOWED_GROUPS, or "
            "OIDC_ALLOW_ALL_USERS=true (an empty allowlist never means everyone)"
        )
    if config.session_days < 1:
        problems.append(f"AUTH_SESSION_DAYS must be at least 1 (got {config.session_days})")
    if problems:
        raise AuthConfigError(
            "AUTH_MODE=oidc, but sign-in cannot be enabled: "
            + "; ".join(problems)
            + ". The core refuses to start rather than run with a door that is not configured."
        )

    _warn_about(config, public_url=public_url)
    return config


def _warn_about(config: AuthConfig, *, public_url: str) -> None:
    """The choices that are allowed but worth a line in the startup log."""
    public = urlsplit(public_url)
    if public.scheme.lower() == "http" and not is_loopback_host(public.hostname):
        log.warning(
            "sign-in is served over plain http on a non-loopback host: the session cookie "
            "cannot be marked Secure and travels in clear text — serve epicurus over https",
            public_url=public_url,
        )
    if public.path not in ("", "/"):
        log.warning(
            "OAUTH_REDIRECT_BASE_URL carries a path; sign-in assumes epicurus is served at "
            "the root of its origin (the shell and its cookies are)",
            public_url=public_url,
        )
    issuer = urlsplit(config.issuer)
    if issuer.scheme.lower() == "http" and not is_loopback_host(issuer.hostname):
        log.warning(
            "the OIDC issuer is plain http on a non-loopback host: codes and tokens cross the "
            "network in clear text — use the provider's https address",
            issuer=config.issuer,
        )
    if config.allow_all_users and (config.allowed_emails or config.allowed_groups):
        log.warning(
            "OIDC_ALLOW_ALL_USERS=true admits everyone the provider authenticates for this "
            "client; OIDC_ALLOWED_EMAILS / OIDC_ALLOWED_GROUPS are ignored"
        )
