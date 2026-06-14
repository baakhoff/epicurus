"""No two compose fragments may publish the same host port (#113, #68).

The fast-gate mirror of the smoke gate's published-port preflight: it parses the
fragments directly (no Docker), so a collision fails in seconds rather than after
an eight-minute stack boot. Source of truth is the scanner in
``scripts/new_module.py`` — the same one ``task new-module`` uses to assign ports.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]


def test_no_duplicate_published_host_ports(new_module: ModuleType) -> None:
    by_port: dict[int, list[str]] = defaultdict(list)
    for port, fragment in new_module.port_bindings(REPO):
        by_port[port].append(fragment)
    dupes = {port: sorted(frags) for port, frags in by_port.items() if len(frags) > 1}
    assert not dupes, f"two fragments publish the same host port — pick a unique one: {dupes}"


def test_scanner_finds_the_known_bindings(new_module: ModuleType) -> None:
    # Guard against the regex silently matching nothing, which would make the
    # collision check vacuously pass.
    used = new_module.published_ports(REPO)
    assert used.get(8082, "").endswith("core-app/compose.yaml")
    assert 8080 in used  # echo
    assert 8200 in used  # OpenBao, from the infra data plane
