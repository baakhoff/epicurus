"""SSRF guard and bounded fetching for operator-supplied URLs (#739).

``link_ingest`` is the first tool in the platform that fetches an arbitrary URL *from
inside the Docker network*, where every other service — the core, Postgres, Valkey,
Qdrant, OpenBao, the docker proxy — is reachable without authentication.  A link the
operator pasted (or that an agent lifted out of a page) is therefore untrusted input
pointed at a privileged network position, and nothing server-side existed to reuse.

The policy, in order:

1. **Scheme** — ``http`` / ``https`` only.  No ``file:``, ``gopher:``, ``ftp:``, and
   no ``data:``.
2. **No credentials in the URL** — a ``user:pass@host`` netloc is refused outright.
   The tool never authenticates to anything (#739's honesty rule), so a URL that
   carries credentials is a request to do something we will not do.
3. **Hostname shape** — a single-label host (``core-app``, ``nats``, ``qdrant``) is
   refused: on this network that *is* an internal service, and a public URL always
   carries a dot.  So are the internal suffixes (``.localhost``, ``.local``,
   ``.internal``, ``.localdomain``, ``.home.arpa``) and ``.onion``.
4. **Address** — the host is resolved and **every** returned address is checked
   against the private / loopback / link-local / reserved / multicast / CGNAT
   ranges, in both address families, with IPv4-mapped and 6to4-embedded IPv6
   unwrapped first so ``::ffff:127.0.0.1`` cannot smuggle a loopback through.  A
   host that resolves to a mix of public and private addresses is refused, not
   partially allowed.
5. **Every redirect hop is re-validated** — redirects are followed manually
   (``follow_redirects=False``) and each ``Location`` goes through steps 1-4 again
   before it is requested.  A public URL that 302s to ``http://169.254.169.254/`` is
   the classic SSRF, and it is the hop that matters, not the entry point.
6. **Caps** — total bytes, total wall-clock across all hops, redirect count, and an
   allow-list of content types.  Over-long bodies are truncated (and flagged) rather
   than failing the fetch, so a huge article still yields its opening; a truncated
   *image* is refused by the caller, since half a JPEG is not an image.

**Residual limitation, stated plainly.** The check resolves the hostname and then lets
httpx resolve it again for the connection, so a DNS entry that changes between the two
(a rebinding attack) is not caught.  Closing that needs connect-time pinning of the
validated address, which httpx does not expose without a custom transport; it is a
deliberate v1 gap, not an oversight.  Every non-DNS vector above *is* closed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

# Suffixes that only ever name something inside a private network. ``.onion`` is not
# internal but is unreachable without Tor, so refusing it early beats a 20s timeout.
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".onion",
)

# Named hosts refused regardless of what DNS says. Every entry is single-label and so is
# already covered by the no-dot rule — they are listed for documentation value and to keep
# the refusal message specific ("an internal service name") for the ones that matter most.
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "core-app",
        "nats",
        "postgres",
        "valkey",
        "qdrant",
        "openbao",
        "searxng",
        "traefik",
        "minio",
        "ollama",
        "prometheus",
        "grafana",
        "loki",
        "alertmanager",
        "docker-proxy-core",
        "instance-data",
        "metadata",
    }
)

# Ranges Python's own classifiers miss or classify inconsistently across versions. Explicit
# so the policy is the same on 3.11 and 3.13: shared/CGNAT space, IETF protocol assignments,
# benchmarking, documentation, and the v6 transition ranges that embed a v4 address.
_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this network"
        "100.64.0.0/10",  # CGNAT / shared address space
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "240.0.0.0/4",  # reserved
        "64:ff9b::/96",  # NAT64
        "100::/64",  # discard-only
        "2001::/32",  # Teredo
        "2001:20::/28",  # ORCHIDv2
        "2002::/16",  # 6to4
        "fc00::/7",  # unique-local
        "fe80::/10",  # link-local
    )
)

# What ``link_ingest`` is willing to read. Anything else (a PDF, a zip, a video stream) is
# refused with its type named, rather than downloaded and guessed at.
HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = ("text/plain", "text/vtt", "application/json", "application/xml", "text/xml")
IMAGE_TYPES = ("image/",)
DEFAULT_ALLOWED_TYPES = (*HTML_TYPES, *TEXT_TYPES, *IMAGE_TYPES)

Resolver = Callable[[str], Awaitable[Sequence[str]]]
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UrlRefused(Exception):
    """The guard will not fetch this URL. ``reason`` is written for the operator."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FetchFailed(Exception):
    """The fetch was allowed but did not succeed. ``reason`` is written for the operator."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def _dns_resolve(host: str) -> Sequence[str]:
    """Every address ``host`` resolves to, as strings. Blocking call, off the event loop."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _unwrap(ip: IpAddress) -> IpAddress:
    """Peel an IPv4 address out of its IPv6 wrapper so it is checked as what it is.

    ``::ffff:127.0.0.1`` (v4-mapped) and ``2002:7f00:1::`` (6to4) both address a v4 host;
    left wrapped, neither is ``is_loopback`` nor ``is_private`` and both would sail through.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour):
            if embedded is not None:
                return embedded
    return ip


def address_is_blocked(raw: str) -> bool:
    """Whether ``raw`` (a literal address) is somewhere ``link_ingest`` must not reach."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        # Not an address at all — the hostname rules already ran; nothing to judge here.
        return False
    ip = _unwrap(ip)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return any(ip.version == net.version and ip in net for net in _EXTRA_BLOCKED)


class UrlGuard:
    """Decides whether a URL may be fetched. One instance is reused for every hop.

    ``resolve`` is injectable so tests exercise the address policy without DNS (and without
    a network round-trip per case); production uses :func:`_dns_resolve`.
    """

    def __init__(self, *, resolve: Resolver | None = None) -> None:
        self._resolve = resolve or _dns_resolve

    async def check(self, url: str) -> None:
        """Raise :class:`UrlRefused` unless ``url`` is safe to fetch."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise UrlRefused(
                f"only http and https links can be read (this one is {scheme or 'schemeless'})"
            )
        if "@" in parts.netloc:
            raise UrlRefused("links carrying credentials are refused — nothing here ever signs in")
        host = (parts.hostname or "").strip().rstrip(".").lower()
        if not host:
            raise UrlRefused("the link has no host")
        if host in _BLOCKED_HOSTS or (not _is_ip_literal(host) and "." not in host):
            raise UrlRefused(f"{host!r} is an internal service name, not a public address")
        if host.endswith(_BLOCKED_SUFFIXES):
            raise UrlRefused(f"{host!r} is an internal or unroutable name")
        if _is_ip_literal(host):
            if address_is_blocked(host):
                raise UrlRefused(f"{host} is a private or reserved address")
            return
        try:
            addresses = await self._resolve(host)
        except OSError as exc:
            raise UrlRefused(f"{host!r} could not be resolved") from exc
        if not addresses:
            raise UrlRefused(f"{host!r} could not be resolved")
        # Every address, not just the first: a host that answers with one public and one
        # private address is a redirect-free way to reach the private one.
        for address in addresses:
            if address_is_blocked(address):
                raise UrlRefused(f"{host!r} resolves to {address}, a private or reserved address")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class FetchLimits:
    """The caps every guarded fetch runs under. Defaults mirror ``WebSearchSettings``."""

    max_bytes: int = 5_000_000
    timeout_s: float = 20.0
    max_redirects: int = 5
    user_agent: str = "epicurus-websearch (+link_ingest)"


@dataclass(frozen=True)
class Fetched:
    """One successfully fetched body.

    ``url`` is the *final* URL after redirects — what the result should cite, since that is
    what was actually read. ``truncated`` says the body hit ``max_bytes`` and is a prefix.
    """

    url: str
    content_type: str
    body: bytes
    truncated: bool = False
    hops: tuple[str, ...] = field(default=())

    def text(self, limit: int | None = None) -> str:
        """The body decoded as text, replacing anything undecodable rather than raising."""
        decoded = self.body.decode("utf-8", errors="replace")
        return decoded if limit is None else decoded[:limit]


class GuardedFetcher:
    """Fetches a URL under :class:`UrlGuard` and :class:`FetchLimits`.

    Holds one ``httpx.AsyncClient`` with redirects **off** — the redirect chain is walked
    here so each hop can be re-validated, which ``follow_redirects=True`` would skip.
    ``transport`` is injectable so tests drive whole redirect chains without a network.
    """

    def __init__(
        self,
        *,
        guard: UrlGuard | None = None,
        limits: FetchLimits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.limits = limits or FetchLimits()
        self.guard = guard or UrlGuard()
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self.limits.timeout_s),
            transport=transport,
            headers={"User-Agent": self.limits.user_agent},
        )

    async def fetch(
        self,
        url: str,
        *,
        accept: str = "*/*",
        allowed_types: Sequence[str] = DEFAULT_ALLOWED_TYPES,
    ) -> Fetched:
        """Fetch ``url``, following (and re-validating) redirects up to the cap.

        Raises :class:`UrlRefused` when the guard says no — including for a redirect target
        and for a disallowed content type — and :class:`FetchFailed` for a timeout, a
        transport error, or a non-2xx status.
        """
        try:
            async with asyncio.timeout(self.limits.timeout_s):
                return await self._fetch(url, accept=accept, allowed_types=allowed_types)
        except TimeoutError as exc:
            raise FetchFailed(f"the page took longer than {self.limits.timeout_s:g}s") from exc

    async def _fetch(self, url: str, *, accept: str, allowed_types: Sequence[str]) -> Fetched:
        current = url
        hops: list[str] = []
        for _ in range(self.limits.max_redirects + 1):
            await self.guard.check(current)
            hops.append(current)
            try:
                request = self._client.build_request("GET", current, headers={"Accept": accept})
                response = await self._client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise FetchFailed(f"could not reach the page ({type(exc).__name__})") from exc
            try:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise FetchFailed("the page redirected without saying where")
                    # Re-validate on the next pass: a public entry point that redirects into
                    # the private network is the SSRF this guard exists for.
                    current = urljoin(current, location)
                    continue
                if response.status_code in (401, 403):
                    raise FetchFailed(
                        f"the page is behind a login wall or blocks automated readers "
                        f"(HTTP {response.status_code})"
                    )
                if response.status_code >= 400:
                    raise FetchFailed(f"the page returned HTTP {response.status_code}")
                content_type = _bare_type(response.headers.get("content-type", ""))
                if allowed_types and not content_type.startswith(tuple(allowed_types)):
                    raise UrlRefused(
                        f"{content_type or 'an unnamed content type'} is not something"
                        " link_ingest reads"
                    )
                body, truncated = await _read_capped(response, self.limits.max_bytes)
                return Fetched(
                    url=str(response.url),
                    content_type=content_type,
                    body=body,
                    truncated=truncated,
                    hops=tuple(hops),
                )
            finally:
                await response.aclose()
        raise UrlRefused(f"the link redirected more than {self.limits.max_redirects} times")

    async def aclose(self) -> None:
        await self._client.aclose()


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Stream at most ``max_bytes`` from ``response``; the flag says whether it was cut."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        remaining = max_bytes - total
        if len(chunk) >= remaining:
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False


def _bare_type(header: str) -> str:
    """``"text/html; charset=utf-8"`` → ``"text/html"``."""
    return header.split(";", 1)[0].strip().lower()
