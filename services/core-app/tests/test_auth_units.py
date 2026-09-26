"""Sign-in (#969), the parts that need no provider: settings, admission, ``next``, the store.

The flow itself — a fake OpenID Connect provider, the routes, the middleware and the real app —
is in ``test_auth_flow.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from structlog.testing import capture_logs

from epicurus_core_app.auth.admission import admit, claim_groups
from epicurus_core_app.auth.config import (
    AuthConfig,
    AuthConfigError,
    is_loopback_host,
    load_auth_config,
    origin_of,
    parse_list,
    parse_scopes,
)
from epicurus_core_app.auth.errors import AUTH_ERROR_CODES
from epicurus_core_app.auth.service import (
    clear_cookie_header,
    cookie_header,
    read_cookie,
    safe_next,
)
from epicurus_core_app.auth.store import MAX_PENDING_LOGINS, AuthStore, hash_token
from epicurus_core_app.portability.core_data import CORE_SETS, EXCLUSIONS
from epicurus_core_app.settings import CoreAppSettings

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _settings(**overrides: Any) -> CoreAppSettings:
    values: dict[str, Any] = {
        "auth_mode": "oidc",
        "oidc_issuer_url": "https://id.example.com",
        "oidc_client_id": "epicurus",
        "oidc_allowed_emails": "me@example.com",
        "oauth_redirect_base_url": "https://epicurus.example.com",
    }
    values.update(overrides)
    return CoreAppSettings(**values)


def _config(**overrides: Any) -> AuthConfig:
    return load_auth_config(_settings(**overrides))


# ── the error vocabulary ──────────────────────────────────────────────────────


def test_the_auth_error_codes_are_exactly_the_contract_the_shell_renders() -> None:
    assert set(AUTH_ERROR_CODES) == {
        "provider_unreachable",
        "provider_error",
        "access_denied",
        "state_mismatch",
        "token_exchange_failed",
        "invalid_token",
        "not_allowed",
        "groups_claim_missing",
        "email_unverified",
        "misconfigured",
    }


# ── settings ──────────────────────────────────────────────────────────────────


def test_the_default_is_no_sign_in_and_nothing_is_validated() -> None:
    config = load_auth_config(
        CoreAppSettings(oauth_redirect_base_url="not a url", oidc_issuer_url="")
    )
    assert config.mode == "none" and not config.enabled


@pytest.mark.parametrize("raw", ["OIDC", " oidc ", "Oidc"])
def test_auth_mode_is_case_and_space_insensitive(raw: str) -> None:
    assert CoreAppSettings(auth_mode=raw).auth_mode == "oidc"


def test_a_blank_auth_mode_is_none() -> None:
    assert CoreAppSettings(auth_mode="").auth_mode == "none"


def test_an_unknown_auth_mode_fails_rather_than_reading_as_none() -> None:
    with pytest.raises(ValidationError, match="auth_mode"):
        CoreAppSettings(auth_mode="oauth")


def test_blank_bool_and_int_values_fall_back_to_their_defaults() -> None:
    settings = CoreAppSettings(
        oidc_allow_all_users="", oidc_auto_redirect=" ", auth_session_days=""
    )
    assert settings.oidc_allow_all_users is False
    assert settings.oidc_auto_redirect is False
    assert settings.auth_session_days == 30


def test_env_names_are_the_documented_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER_URL": "https://id.example.com/",
        "OIDC_CLIENT_ID": "cid",
        "OIDC_CLIENT_SECRET": "s3cret",
        "OIDC_SCOPES": "email,groups",
        "OIDC_PROVIDER_NAME": "Pocket ID",
        "OIDC_ALLOWED_EMAILS": "A@Example.com, b@example.com",
        "OIDC_ALLOWED_GROUPS": "family, Home Users",
        "OIDC_ALLOW_ALL_USERS": "false",
        "OIDC_AUTO_REDIRECT": "true",
        "AUTH_SESSION_DAYS": "7",
        "OAUTH_REDIRECT_BASE_URL": "https://epicurus.example.com/",
    }.items():
        monkeypatch.setenv(name, value)
    config = load_auth_config(CoreAppSettings())
    assert config.enabled
    assert config.issuer == "https://id.example.com/"
    assert config.client_id == "cid" and config.client_secret == "s3cret"
    assert config.scopes == ("openid", "email", "groups")
    assert config.provider_name == "Pocket ID"
    assert config.allowed_emails == {"a@example.com", "b@example.com"}
    assert config.allowed_groups == {"family", "Home Users"}
    assert config.auto_redirect is True and config.session_days == 7
    # Derived, never configured — and a trailing slash on the base does not double up.
    assert config.redirect_uri == "https://epicurus.example.com/platform/v1/auth/callback"
    assert config.public_origin == "https://epicurus.example.com"
    assert config.secure_cookies is True


def test_the_client_secret_is_never_in_a_repr() -> None:
    config = _config(oidc_client_secret="hunter2")
    assert "hunter2" not in repr(config)
    assert "hunter2" not in repr(_settings(oidc_client_secret="hunter2"))


def test_startup_names_everything_that_is_missing_at_once() -> None:
    with pytest.raises(AuthConfigError) as err:
        load_auth_config(_settings(oidc_issuer_url="", oidc_client_id=" ", oidc_allowed_emails=""))
    message = str(err.value)
    assert "OIDC_ISSUER_URL is not set" in message
    assert "OIDC_CLIENT_ID is not set" in message
    assert "OIDC_ALLOWED_EMAILS" in message and "OIDC_ALLOW_ALL_USERS=true" in message


@pytest.mark.parametrize(
    "rule",
    [
        {"oidc_allowed_emails": "me@example.com"},
        {"oidc_allowed_groups": "family"},
        {"oidc_allow_all_users": True},
    ],
)
def test_any_one_admission_rule_is_enough(rule: dict[str, Any]) -> None:
    overrides: dict[str, Any] = {"oidc_allowed_emails": ""}
    overrides.update(rule)
    assert _config(**overrides).enabled


def test_an_allowlist_of_only_commas_is_no_rule_at_all() -> None:
    with pytest.raises(AuthConfigError, match="no admission rule"):
        _config(oidc_allowed_emails=" , ,")


@pytest.mark.parametrize(
    ("base", "fragment"),
    [
        ("", "OAUTH_REDIRECT_BASE_URL is not set"),
        ("epicurus.example.com", "absolute http(s) URL"),
        ("ftp://epicurus.example.com", "absolute http(s) URL"),
        ("https://epicurus.example.com/?x=1", "query string"),
    ],
)
def test_the_public_url_must_be_an_absolute_http_url(base: str, fragment: str) -> None:
    with pytest.raises(AuthConfigError, match="OAUTH_REDIRECT_BASE_URL") as err:
        _config(oauth_redirect_base_url=base)
    assert fragment in str(err.value)


def test_the_issuer_must_be_an_absolute_http_url() -> None:
    with pytest.raises(AuthConfigError, match="OIDC_ISSUER_URL must be an absolute"):
        _config(oidc_issuer_url="id.example.com")


def test_session_days_below_one_fails() -> None:
    with pytest.raises(AuthConfigError, match="AUTH_SESSION_DAYS"):
        _config(auth_session_days=0)


def test_plain_http_on_a_real_host_warns_but_starts() -> None:
    with capture_logs() as logs:
        config = _config(oauth_redirect_base_url="http://epicurus.lan:8084")
    assert config.enabled and config.secure_cookies is False
    assert any("plain http on a non-loopback host" in e["event"] for e in logs)
    assert all(e["log_level"] == "warning" for e in logs)


def test_plain_http_on_loopback_does_not_warn() -> None:
    with capture_logs() as logs:
        config = _config(oauth_redirect_base_url="http://localhost:8084")
    assert config.secure_cookies is False
    assert logs == []


def test_allow_all_users_beside_an_allowlist_warns_that_the_list_is_ignored() -> None:
    with capture_logs() as logs:
        _config(oidc_allow_all_users=True)
    assert any("are ignored" in e["event"] for e in logs)


def test_parse_scopes_always_leads_with_openid_and_deduplicates() -> None:
    assert parse_scopes("email profile") == ("openid", "email", "profile")
    assert parse_scopes("profile,openid, groups groups") == ("openid", "profile", "groups")
    assert parse_scopes("  ") == ("openid", "email", "profile")


def test_parse_list_trims_and_optionally_lowercases() -> None:
    assert parse_list(" A@x.com ,, b@x.com", lower=True) == {"a@x.com", "b@x.com"}
    assert parse_list("Family , Home Users") == {"Family", "Home Users"}


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Example.com:443/x", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        ("http://example.com:8084/", "http://example.com:8084"),
        ("http://[::1]:8084", "http://[::1]:8084"),
        ("null", None),
        ("ftp://example.com", None),
    ],
)
def test_origin_of(url: str, origin: str | None) -> None:
    assert origin_of(url) == origin


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("localhost", True),
        ("core-app.localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("192.168.1.10", False),
        ("epicurus.lan", False),
        (None, False),
    ],
)
def test_is_loopback_host(host: str | None, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback


# ── admission ─────────────────────────────────────────────────────────────────


def _rule(emails: str = "", groups: str = "", everyone: bool = False) -> AuthConfig:
    return _config(
        oidc_allowed_emails=emails, oidc_allowed_groups=groups, oidc_allow_all_users=everyone
    )


def test_allow_all_users_admits_anyone() -> None:
    assert admit({"sub": "x"}, _rule(everyone=True)) is None


def test_email_matches_case_insensitively() -> None:
    assert admit({"email": " Me@Example.COM "}, _rule(emails="me@example.com")) is None


@pytest.mark.parametrize("verified", [True, "true", None])
def test_an_email_not_explicitly_unverified_admits(verified: object) -> None:
    claims: dict[str, Any] = {"email": "me@example.com"}
    if verified is not None:
        claims["email_verified"] = verified
    assert admit(claims, _rule(emails="me@example.com")) is None


@pytest.mark.parametrize("verified", [False, "false", "FALSE"])
def test_an_unverified_allowlisted_email_is_refused_as_such(verified: object) -> None:
    claims = {"email": "me@example.com", "email_verified": verified}
    assert admit(claims, _rule(emails="me@example.com")) == "email_unverified"


def test_an_unlisted_email_is_not_allowed() -> None:
    assert admit({"email": "else@example.com"}, _rule(emails="me@example.com")) == "not_allowed"


def test_groups_as_a_list_or_a_single_string_both_match() -> None:
    config = _rule(groups="family")
    assert admit({"groups": ["guests", "family"]}, config) is None
    assert admit({"groups": "family"}, config) is None


def test_groups_match_exactly() -> None:
    assert admit({"groups": ["Family"]}, _rule(groups="family")) == "not_allowed"


def test_no_groups_claim_at_all_is_named() -> None:
    assert admit({"email": "x@y.z"}, _rule(groups="family")) == "groups_claim_missing"


def test_an_empty_groups_claim_is_simply_not_allowed() -> None:
    assert admit({"groups": []}, _rule(groups="family")) == "not_allowed"


def test_groups_admit_even_when_the_listed_email_is_unverified() -> None:
    claims = {"email": "me@example.com", "email_verified": False, "groups": ["family"]}
    assert admit(claims, _rule(emails="me@example.com", groups="family")) is None


def test_email_unverified_outranks_a_missing_groups_claim() -> None:
    claims = {"email": "me@example.com", "email_verified": False}
    assert admit(claims, _rule(emails="me@example.com", groups="family")) == "email_unverified"


def test_claim_groups_drops_non_strings_and_tells_absent_from_empty() -> None:
    assert claim_groups({}) is None
    assert claim_groups({"groups": None}) is None
    assert claim_groups({"groups": ["a", 3, "", "b"]}) == ["a", "b"]
    assert claim_groups({"groups": {"a": 1}}) == []


# ── next, cookies ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "chat",
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example/",
        "/a\\b",
        "/%2F%2Fevil.example",
        "/platform/v1/auth/logout",
        "/platform/v1/auth",
        "/platform/v1/%61uth/login",
        "/x\r\nSet-Cookie: a=b",
        "/" + "a" * 2048,
        "javascript:alert(1)",
    ],
)
def test_safe_next_refuses_anything_but_a_same_origin_path(raw: str | None) -> None:
    assert safe_next(raw) == "/"


@pytest.mark.parametrize("raw", ["/", "/chat", "/chat?session=abc#m-2", "/files/a%20b"])
def test_safe_next_keeps_an_ordinary_path(raw: str) -> None:
    assert safe_next(raw) == raw


def test_cookie_headers_carry_the_security_attributes() -> None:
    header = cookie_header("epicurus_session", "tok", max_age=60, path="/", secure=True)
    assert header == "epicurus_session=tok; Max-Age=60; Path=/; HttpOnly; SameSite=Lax; Secure"
    cleared = clear_cookie_header("epicurus_auth_tx", path="/platform/v1/auth", secure=False)
    assert "Max-Age=0" in cleared and "Expires=Thu, 01 Jan 1970" in cleared
    assert "Secure" not in cleared and "Path=/platform/v1/auth" in cleared


def test_read_cookie_finds_the_value_across_headers_and_junk() -> None:
    headers = ["theme=dark; junk", 'a=1; epicurus_session="tok"']
    assert read_cookie(headers, "epicurus_session") == "tok"
    assert read_cookie(["epicurus_session="], "epicurus_session") is None
    assert read_cookie([], "epicurus_session") is None


# ── the store ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[AuthStore]:
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    auth_store = AuthStore(engine)
    await auth_store.init()
    try:
        yield auth_store
    finally:
        await engine.dispose()


async def _add_login(
    store: AuthStore, state: str, *, tenant: str = "local", at: datetime = T0
) -> None:
    await store.add_login_state(
        tenant=tenant,
        state=state,
        nonce="nonce-value",
        code_verifier="verifier-value",
        next_path="/chat",
        now=at,
        expires_at=at + timedelta(minutes=10),
    )


async def test_a_login_state_is_taken_once(store: AuthStore) -> None:
    await _add_login(store, "s1")
    taken = await store.take_login_state(tenant="local", state="s1")
    assert taken is not None and taken.next_path == "/chat" and taken.nonce == "nonce-value"
    assert taken.expires_at == T0 + timedelta(minutes=10)
    assert await store.take_login_state(tenant="local", state="s1") is None
    assert "nonce-value" not in repr(taken) and "verifier-value" not in repr(taken)


async def test_a_login_state_belongs_to_its_tenant(store: AuthStore) -> None:
    await _add_login(store, "s1", tenant="other")
    assert await store.take_login_state(tenant="local", state="s1") is None


async def test_expired_login_states_are_purged_by_the_next_login(store: AuthStore) -> None:
    await _add_login(store, "old")
    await _add_login(store, "new", at=T0 + timedelta(minutes=11))
    assert await store.take_login_state(tenant="local", state="old") is None
    assert await store.take_login_state(tenant="local", state="new") is not None


async def test_pending_logins_are_capped_oldest_first(
    store: AuthStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("epicurus_core_app.auth.store.MAX_PENDING_LOGINS", 3)
    for i in range(5):
        await _add_login(store, f"s{i}", at=T0 + timedelta(seconds=i))
    remaining = [
        s
        for s in ("s0", "s1", "s2", "s3", "s4")
        if await store.take_login_state(tenant="local", state=s)
    ]
    assert remaining == ["s2", "s3", "s4"]
    assert MAX_PENDING_LOGINS == 1000


async def test_a_session_is_stored_by_hash_and_read_back(store: AuthStore) -> None:
    record = await store.add_session(
        token_hash=hash_token("tok"),
        tenant="local",
        issuer="https://id.example.com",
        subject="u1",
        email="me@example.com",
        name="N" * 400,
        groups=["family"],
        now=T0,
        expires_at=T0 + timedelta(days=30),
    )
    assert record.id == hash_token("tok") != "tok"
    assert record.name is not None and len(record.name) == 255
    got = await store.get_session(tenant="local", token_hash=hash_token("tok"))
    assert got is not None and got.groups == ("family",) and got.expires_at.tzinfo is not None
    assert await store.get_session(tenant="other", token_hash=hash_token("tok")) is None
    later = T0 + timedelta(hours=2)
    assert await store.touch_session(
        tenant="local", token_hash=record.id, last_seen_at=later, expires_at=later
    )
    assert await store.delete_session(tenant="local", token_hash=record.id)
    assert not await store.delete_session(tenant="local", token_hash=record.id)
    assert not await store.touch_session(
        tenant="local", token_hash=record.id, last_seen_at=later, expires_at=later
    )


async def test_purge_removes_only_expired_rows(store: AuthStore) -> None:
    for token, days in (("old", -1), ("live", 1)):
        await store.add_session(
            token_hash=hash_token(token),
            tenant="local",
            issuer="i",
            subject=token,
            email=None,
            name=None,
            groups=[],
            now=T0 - timedelta(days=2),
            expires_at=T0 + timedelta(days=days),
        )
    await _add_login(store, "stale", at=T0 - timedelta(hours=1))
    assert await store.purge_expired(tenant="local", now=T0) == 2
    assert await store.get_session(tenant="local", token_hash=hash_token("live")) is not None


# ── portability ───────────────────────────────────────────────────────────────


def test_sign_in_tables_never_travel_in_a_tenant_archive() -> None:
    travelling = {spec.table.name for specs in CORE_SETS.values() for spec in specs}
    assert not {"auth_sessions", "auth_login_states"} & travelling
    assert any(
        "auth_sessions" in e.component and "auth_login_states" in e.component for e in EXCLUSIONS
    )
