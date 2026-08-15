"""Tests for the SSRF guard and the bounded fetcher (safety.py, #739).

This is the security boundary of ``link_ingest``: the tool fetches an operator-supplied
URL from inside the Docker network, where every other service answers without auth. The
tests below are deliberately exhaustive about *refusals* — a gap here is not a missing
feature, it is a way out of the network.

DNS is injected rather than performed, so every address case runs offline and
deterministically, and the whole redirect machinery runs against ``httpx.MockTransport``.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest

from epicurus_websearch.safety import (
    FetchFailed,
    FetchLimits,
    GuardedFetcher,
    UrlGuard,
    UrlRefused,
    address_is_blocked,
)

PUBLIC = "93.184.216.34"


def _guard(*addresses: str) -> UrlGuard:
    """A guard whose DNS always answers with ``addresses`` (default: one public address)."""
    answer: Sequence[str] = addresses or (PUBLIC,)

    async def resolve(_host: str) -> Sequence[str]:
        return answer

    return UrlGuard(resolve=resolve)


def _failing_guard(exc: Exception) -> UrlGuard:
    async def resolve(_host: str) -> Sequence[str]:
        raise exc

    return UrlGuard(resolve=resolve)


# ── scheme, credentials, shape ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com:70/_test",
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)",
        "//example.com/no-scheme",
    ],
)
async def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(UrlRefused):
        await _guard().check(url)


async def test_credentials_in_the_url_are_refused() -> None:
    """#739's honesty rule made structural: nothing here ever authenticates."""
    with pytest.raises(UrlRefused, match="credentials"):
        await _guard().check("https://user:secret@example.com/page")


async def test_empty_host_is_refused() -> None:
    with pytest.raises(UrlRefused):
        await _guard().check("https:///just-a-path")


@pytest.mark.parametrize(
    "host",
    [
        "core-app",
        "nats",
        "qdrant",
        "openbao",
        "postgres",
        "valkey",
        "searxng",
        "localhost",
        "minio",
        # Any single-label host at all: on this network a dotless name is a service name.
        "some-new-service",
    ],
)
async def test_internal_service_names_are_refused(host: str) -> None:
    with pytest.raises(UrlRefused, match="internal service name"):
        await _guard().check(f"http://{host}:8080/platform/v1/info")


@pytest.mark.parametrize(
    "host",
    [
        "core-app.localhost",
        "printer.local",
        "metadata.google.internal",
        "box.localdomain",
        "thing.home.arpa",
        "abcdefgh.onion",
    ],
)
async def test_internal_suffixes_are_refused(host: str) -> None:
    with pytest.raises(UrlRefused, match="internal or unroutable"):
        await _guard().check(f"http://{host}/x")


async def test_trailing_dot_does_not_bypass_the_host_rules() -> None:
    """``localhost.`` is the same host as ``localhost`` to a resolver — and to us."""
    with pytest.raises(UrlRefused):
        await _guard().check("http://localhost./x")


# ── addresses ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "127.13.37.1",  # the rest of 127/8
        "10.0.0.5",  # private
        "172.16.4.4",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # cloud metadata (link-local)
        "0.0.0.0",  # unspecified
        "0.1.2.3",  # "this network"
        "100.64.0.1",  # CGNAT
        "198.18.0.1",  # benchmarking
        "192.0.2.1",  # TEST-NET-1
        "203.0.113.9",  # TEST-NET-3
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "255.255.255.255",  # broadcast
        "::1",  # v6 loopback
        "fe80::1",  # v6 link-local
        "fc00::1",  # v6 unique-local
        "::ffff:127.0.0.1",  # v4-mapped loopback
        "::ffff:10.0.0.1",  # v4-mapped private
        "2002:7f00:1::",  # 6to4-wrapped 127.0.0.1
        "::",  # v6 unspecified
    ],
)
def test_address_is_blocked_covers_every_reserved_range(address: str) -> None:
    assert address_is_blocked(address) is True


@pytest.mark.parametrize("address", [PUBLIC, "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert address_is_blocked(address) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/",
        "http://[::1]:8080/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
async def test_ip_literals_in_reserved_ranges_are_refused(url: str) -> None:
    with pytest.raises(UrlRefused, match="private or reserved"):
        await _guard().check(url)


async def test_public_ip_literal_is_allowed() -> None:
    await _guard().check(f"http://{PUBLIC}/page")  # no raise


async def test_hostname_resolving_to_a_private_address_is_refused() -> None:
    """The rebinding-adjacent case we *do* catch: a public name pointing inward."""
    with pytest.raises(UrlRefused, match="private or reserved"):
        await _guard("10.0.0.7").check("https://evil.example.com/x")


async def test_any_private_address_among_several_refuses_the_whole_host() -> None:
    """A host answering with one public and one private address is not partially allowed."""
    with pytest.raises(UrlRefused, match="private or reserved"):
        await _guard(PUBLIC, "127.0.0.1").check("https://mixed.example.com/x")


async def test_unresolvable_host_is_refused_not_crashed() -> None:
    with pytest.raises(UrlRefused, match="could not be resolved"):
        await _failing_guard(OSError("no such host")).check("https://nope.example.com/")


async def test_host_resolving_to_nothing_is_refused() -> None:
    async def resolve(_host: str) -> Sequence[str]:
        return []

    with pytest.raises(UrlRefused, match="could not be resolved"):
        await UrlGuard(resolve=resolve).check("https://empty.example.com/")


async def test_ordinary_public_url_passes() -> None:
    await _guard().check("https://example.com/some/article?a=1")  # no raise


# ── the fetcher: redirects, caps, content types ────────────────────────────────────────


def _fetcher(
    handler: object,
    *,
    guard: UrlGuard | None = None,
    limits: FetchLimits | None = None,
) -> GuardedFetcher:
    return GuardedFetcher(
        guard=guard or _guard(),
        limits=limits or FetchLimits(timeout_s=5.0),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_fetch_returns_body_and_bare_content_type() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>hi</html>", headers={"content-type": "text/html; charset=utf-8"}
        )

    fetcher = _fetcher(handler)
    fetched = await fetcher.fetch("https://example.com/a")
    assert fetched.body == b"<html>hi</html>"
    assert fetched.content_type == "text/html"
    assert fetched.truncated is False
    await fetcher.aclose()


async def test_fetch_sends_the_configured_user_agent() -> None:
    """We identify ourselves rather than impersonating a browser."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    fetcher = _fetcher(handler, limits=FetchLimits(user_agent="epicurus-test/1"))
    await fetcher.fetch("https://example.com/a")
    assert seen == ["epicurus-test/1"]
    await fetcher.aclose()


async def test_redirect_to_a_private_address_is_refused_at_the_hop() -> None:
    """The whole point of walking redirects by hand: the *hop* is what must be checked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        raise AssertionError("the metadata endpoint must never be requested")

    fetcher = _fetcher(handler)
    with pytest.raises(UrlRefused, match="private or reserved"):
        await fetcher.fetch("https://example.com/start")
    await fetcher.aclose()


async def test_redirect_to_an_internal_service_name_is_refused_at_the_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "http://core-app:8080/platform/v1"})
        raise AssertionError("the core must never be requested")

    fetcher = _fetcher(handler)
    with pytest.raises(UrlRefused, match="internal service name"):
        await fetcher.fetch("https://example.com/start")
    await fetcher.aclose()


async def test_redirect_to_a_non_http_scheme_is_refused_at_the_hop() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    fetcher = _fetcher(handler)
    with pytest.raises(UrlRefused):
        await fetcher.fetch("https://example.com/start")
    await fetcher.aclose()


async def test_relative_redirects_are_resolved_and_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200, content=b"landed", headers={"content-type": "text/plain"})

    fetcher = _fetcher(handler)
    fetched = await fetcher.fetch("https://example.com/a")
    assert fetched.body == b"landed"
    assert fetched.url.endswith("/b")
    assert fetched.hops == ("https://example.com/a", "https://example.com/b")
    await fetcher.aclose()


async def test_redirect_chain_beyond_the_cap_is_refused() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        step = len(calls)
        return httpx.Response(302, headers={"location": f"https://example.com/hop{step}"})

    fetcher = _fetcher(handler, limits=FetchLimits(max_redirects=2, timeout_s=5.0))
    with pytest.raises(UrlRefused, match="redirected more than 2 times"):
        await fetcher.fetch("https://example.com/start")
    # 1 initial + 2 permitted hops, then a refusal — never a fourth request.
    assert len(calls) == 3
    await fetcher.aclose()


async def test_redirect_without_a_location_fails_cleanly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    fetcher = _fetcher(handler)
    with pytest.raises(FetchFailed, match="without saying where"):
        await fetcher.fetch("https://example.com/start")
    await fetcher.aclose()


async def test_body_over_the_byte_cap_is_truncated_not_failed() -> None:
    """A long article still yields its opening; the flag says it was cut."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5_000, headers={"content-type": "text/plain"})

    fetcher = _fetcher(handler, limits=FetchLimits(max_bytes=1_000, timeout_s=5.0))
    fetched = await fetcher.fetch("https://example.com/big")
    assert len(fetched.body) == 1_000
    assert fetched.truncated is True
    await fetcher.aclose()


async def test_body_exactly_at_the_cap_is_not_flagged_truncated() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 1_000, headers={"content-type": "text/plain"})

    fetcher = _fetcher(handler, limits=FetchLimits(max_bytes=1_001, timeout_s=5.0))
    fetched = await fetcher.fetch("https://example.com/exact")
    assert fetched.truncated is False
    await fetcher.aclose()


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "application/zip", "video/mp4", "application/octet-stream"],
)
async def test_disallowed_content_types_are_refused(content_type: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"...", headers={"content-type": content_type})

    fetcher = _fetcher(handler)
    with pytest.raises(UrlRefused, match="not something link_ingest reads"):
        await fetcher.fetch("https://example.com/file")
    await fetcher.aclose()


async def test_empty_allowed_types_skips_the_content_type_check() -> None:
    """Subtitle tracks come back as anything; the byte cap and the guard still apply."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"WEBVTT", headers={"content-type": "video/vtt"})

    fetcher = _fetcher(handler)
    fetched = await fetcher.fetch("https://example.com/subs", allowed_types=())
    assert fetched.body == b"WEBVTT"
    await fetcher.aclose()


@pytest.mark.parametrize(("status", "match"), [(401, "login wall"), (403, "login wall")])
async def test_auth_statuses_report_a_login_wall(status: int, match: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    fetcher = _fetcher(handler)
    with pytest.raises(FetchFailed, match=match):
        await fetcher.fetch("https://example.com/private")
    await fetcher.aclose()


async def test_server_error_is_reported_with_its_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    fetcher = _fetcher(handler)
    with pytest.raises(FetchFailed, match="HTTP 503"):
        await fetcher.fetch("https://example.com/down")
    await fetcher.aclose()


async def test_transport_error_becomes_a_clean_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fetcher = _fetcher(handler)
    with pytest.raises(FetchFailed, match="could not reach the page"):
        await fetcher.fetch("https://example.com/gone")
    await fetcher.aclose()


async def test_the_guard_runs_before_any_request_is_made() -> None:
    """A refused URL must never reach the transport at all."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been sent")

    fetcher = _fetcher(handler)
    with pytest.raises(UrlRefused):
        await fetcher.fetch("http://127.0.0.1:5432/")
    await fetcher.aclose()


async def test_slow_response_hits_the_wall_clock_cap() -> None:
    import asyncio

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, content=b"late", headers={"content-type": "text/plain"})

    fetcher = _fetcher(handler, limits=FetchLimits(timeout_s=0.2))
    with pytest.raises(FetchFailed, match="took longer than"):
        await fetcher.fetch("https://example.com/slow")
    await fetcher.aclose()
