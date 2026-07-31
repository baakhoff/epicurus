"""Regression guard on the Docker-socket-proxy allowlists (#726).

#714/ADR-0109 made least-privilege Docker control the default: `docker-proxy-core` and
`docker-proxy-traefik` each carry an exact method+path allowlist instead of the raw
socket. #724 added a third, `docker-proxy-observability`. Nothing else guards those
allowlists against drift — a well-meaning edit that adds `-allowPOST` for a new
feature, or publishes a port on a proxy, passes every other gate: `compose-validate`
only lints YAML (and never even sees `docker-proxy-observability` — it renders without
the `observability` profile, so a profile-gated service is dropped from that output
entirely, not just hidden), and `runtime-smoke` proves Docker control *works*, never
that it is still *narrow*.

This renders the real `docker compose config` — the `observability` profile enabled,
so all three proxies are visible — and asserts, per proxy: the exact allowed
method+path set (an equality check, so an addition fails as loudly as a removal), the
exact `-allowfrom` source restriction, no published host port, the container-hardening
flags (ADR-0109 §3) that are as much the boundary as the allowlist itself, and that the
raw socket is mounted nowhere but the three proxies, always read-only. Widening any of
these now means deleting an assertion here — a deliberate, reviewable diff — rather
than a side effect of adding a flag elsewhere.

Mirrors ``tests/test_compose_ports.py``'s own pattern of parsing the *rendered* config,
because that's what would actually run — except here the source of truth has to be
`docker compose config` itself (not the fragments by hand): only the merged, resolved
config can be checked against the observability profile at all.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SOCKET_PATH = "/var/run/docker.sock"

# The complete method -> {path regex, ...} grant each proxy is supposed to carry, read
# straight off the compose fragment that defines it. An addition or removal on either
# side (here or in compose) is a deliberate, reviewable diff, not silent drift.
EXPECTED_ALLOWLISTS: dict[str, dict[str, frozenset[str]]] = {
    # services/core-app/compose.yaml — paired with core-app; the only proxy with any
    # write grant at all (list/inspect/stop/restart/remove a container by id).
    "docker-proxy-core": {
        "GET": frozenset(
            {
                r"/(v[0-9]+\.[0-9]+/)?_ping",
                r"/(v[0-9]+\.[0-9]+/)?version",
                r"/(v[0-9]+\.[0-9]+/)?containers/json",
                r"/(v[0-9]+\.[0-9]+/)?containers/[^/]+/json",
            }
        ),
        "POST": frozenset(
            {
                r"/(v[0-9]+\.[0-9]+/)?containers/[^/]+/stop",
                r"/(v[0-9]+\.[0-9]+/)?containers/[^/]+/restart",
            }
        ),
        "DELETE": frozenset(
            {
                r"/(v[0-9]+\.[0-9]+/)?containers/[^/]+",
            }
        ),
    },
    # infra/edge/compose.yaml — paired with the edge gateway; read-only (the gateway
    # terminates every inbound request, so it never gets the restart/remove grant).
    "docker-proxy-traefik": {
        "GET": frozenset(
            {
                r"/(v[0-9]+\.[0-9]+/)?_ping",
                r"/(v[0-9]+\.[0-9]+/)?version",
                r"/(v[0-9]+\.[0-9]+/)?containers/json",
                r"/(v[0-9]+\.[0-9]+/)?containers/[^/]+/json",
                r"/(v[0-9]+\.[0-9]+/)?events",
            }
        ),
    },
    # infra/observability/compose.yaml — paired with Prometheus (container-label
    # service discovery) and Alloy (container discovery + log tailing); read-only. The
    # HEAD ping and the `networks`/`logs` GETs are real calls both consumers make,
    # confirmed against a live boot (#724) rather than assumed by analogy to
    # docker-proxy-traefik — neither call exists in that proxy's allowlist, and without
    # them here Prometheus's own discovery refresh errors outright.
    "docker-proxy-observability": {
        "GET": frozenset(
            {
                r"/(v[0-9]+\.[0-9]+/)?_ping",
                r"/(v[0-9]+\.[0-9]+/)?version",
                r"/(v[0-9]+\.[0-9]+/)?containers/json",
                r"/(v[0-9]+\.[0-9]+/)?containers/[^/]+/json",
                r"/(v[0-9]+\.[0-9]+/)?containers/[^/]+/logs",
                r"/(v[0-9]+\.[0-9]+/)?networks",
            }
        ),
        "HEAD": frozenset(
            {
                r"/(v[0-9]+\.[0-9]+/)?_ping",
            }
        ),
    },
}

# The exact `-allowfrom` source restriction each proxy carries — as much a part of the
# grant as the method+path allowlist: widening *who* may call is the same class of
# silent-drift risk as widening *what* may be called.
EXPECTED_ALLOWFROM: dict[str, str] = {
    "docker-proxy-core": "core-app",
    "docker-proxy-traefik": "gateway",
    "docker-proxy-observability": "prometheus,alloy",
}

_ALLOW_FLAG = re.compile(r"^-allow(GET|HEAD|POST|PUT|PATCH|DELETE|CONNECT|TRACE|OPTIONS)=(.+)$")


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    """Every service in the rendered stack, ``observability`` profile included.

    Profile-gated services — `docker-proxy-observability` and its two consumers all
    live behind `observability` (#724) — are dropped from the render entirely unless
    the profile is active, not merely filtered from `up`/`ps`. Without this, every
    assertion below would vacuously pass against an empty set. This is also exactly why
    `compose-validate`'s plain `docker compose config -q` never exercises them (the gap
    #724 names explicitly).
    """
    result = subprocess.run(
        ["docker", "compose", "-f", str(REPO / "compose.yaml"), "config"],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, "COMPOSE_PROFILES": "observability"},
    )
    assert result.returncode == 0, f"docker compose config failed:\n{result.stderr}"
    config: dict[str, Any] = yaml.safe_load(result.stdout)
    return config["services"]  # type: ignore[no-any-return]


def _command_allowlist(service: dict[str, Any]) -> dict[str, set[str]]:
    """The ``{method: {path regex, ...}}`` a proxy service's ``command:`` grants."""
    allowed: dict[str, set[str]] = defaultdict(set)
    for arg in service.get("command") or []:
        m = _ALLOW_FLAG.match(str(arg))
        if m:
            allowed[m.group(1)].add(m.group(2))
    return dict(allowed)


def _allowfrom(service: dict[str, Any]) -> str | None:
    for arg in service.get("command") or []:
        text = str(arg)
        if text.startswith("-allowfrom="):
            return text.split("=", 1)[1]
    return None


def _socket_mounts(service: dict[str, Any]) -> list[dict[str, Any]]:
    """Bind mounts of the raw Docker socket on a rendered service, if any."""
    return [
        v
        for v in service.get("volumes") or []
        if v.get("type") == "bind" and v.get("source") == SOCKET_PATH
    ]


def test_known_proxies_are_exactly_these(services: dict[str, Any]) -> None:
    """A renamed, added, or removed proxy shows up here first, by name."""
    actual = {name for name in services if name.startswith("docker-proxy-")}
    assert actual == set(EXPECTED_ALLOWLISTS)


@pytest.mark.parametrize("proxy_name", sorted(EXPECTED_ALLOWLISTS))
def test_proxy_allowlist_is_exact(services: dict[str, Any], proxy_name: str) -> None:
    """Equality, not subset — an added grant fails this as loudly as a removed one."""
    assert proxy_name in services, f"'{proxy_name}' is missing from the rendered stack"
    actual = _command_allowlist(services[proxy_name])
    assert actual == EXPECTED_ALLOWLISTS[proxy_name]


@pytest.mark.parametrize("proxy_name", sorted(EXPECTED_ALLOWFROM))
def test_proxy_allowfrom_is_exact(services: dict[str, Any], proxy_name: str) -> None:
    assert _allowfrom(services[proxy_name]) == EXPECTED_ALLOWFROM[proxy_name]


@pytest.mark.parametrize("proxy_name", sorted(EXPECTED_ALLOWLISTS))
def test_proxy_publishes_no_host_port(services: dict[str, Any], proxy_name: str) -> None:
    assert not services[proxy_name].get("ports"), (
        f"'{proxy_name}' publishes a host port — it must stay internal-only"
    )


@pytest.mark.parametrize("proxy_name", sorted(EXPECTED_ALLOWLISTS))
def test_proxy_hardening_flags_hold(services: dict[str, Any], proxy_name: str) -> None:
    """The container-level boundary (ADR-0109 §3), guarded the same way as the allowlist.

    The allowlist is the intended security boundary, not the uid (ADR-0109 §3) — but
    only because these flags strip everything else a root-uid process could otherwise
    reach. Losing any one of them silently widens the blast radius without touching a
    single `-allow*` flag.
    """
    svc = services[proxy_name]
    assert svc.get("cap_drop") == ["ALL"], f"'{proxy_name}' no longer drops all capabilities"
    assert svc.get("read_only") is True, f"'{proxy_name}' no longer has a read-only rootfs"
    assert "no-new-privileges:true" in (svc.get("security_opt") or []), (
        f"'{proxy_name}' no longer sets no-new-privileges"
    )


def test_raw_socket_is_mounted_only_into_the_proxies_read_only(services: dict[str, Any]) -> None:
    """Widening this means a new service holding root-equivalent Docker access.

    Scans *every* rendered service rather than asserting on `gateway`/`core-app`/
    `prometheus`/`alloy` by name, so a future service that reaches for the socket
    directly is caught too — not just the four ADR-0109/#724 already named.
    """
    mounting: dict[str, list[dict[str, Any]]] = {
        name: mounts for name, svc in services.items() if (mounts := _socket_mounts(svc))
    }

    assert set(mounting) == set(EXPECTED_ALLOWLISTS), (
        f"the raw socket is mounted into {sorted(mounting)}, expected exactly the "
        f"proxies {sorted(EXPECTED_ALLOWLISTS)}"
    )
    for name, mounts in mounting.items():
        for mount in mounts:
            assert mount.get("read_only") is True, f"'{name}' mounts the raw socket read-write"
