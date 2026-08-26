"""No tracked source file may contain a raw NUL byte (#833).

``services/web/src/components/EventAlertsCard.tsx`` carried exactly one for a long
stretch — a raw NUL used as ``keyOf``'s map-key separator — which made git treat the
whole file as binary and silently drop it from every reviewable diff since the day
it was added. The fix replaced the raw byte with its escaped form (``\\0``,
behavior-identical at runtime); this test is the regression guard so the next one
doesn't slip through the same way undetected. Binary assets (images, fonts, model
weights — see .gitattributes) are legitimately exempt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def _binary_attributed(paths: list[str]) -> set[str]:
    """Paths git's own .gitattributes marks ``binary`` — exempt from the text check."""
    stdin_data = "\0".join(paths) + "\0"
    out = subprocess.run(
        ["git", "check-attr", "--stdin", "-z", "binary"],
        cwd=REPO,
        input=stdin_data.encode("utf-8"),
        capture_output=True,
        check=True,
    ).stdout
    parts = out.decode("utf-8").split("\0")
    triples = [parts[i : i + 3] for i in range(0, len(parts) - 1, 3)]
    return {path for path, _attr, value in triples if value == "set"}


def test_no_tracked_source_file_contains_a_nul_byte() -> None:
    tracked = _tracked_files()
    exempt = _binary_attributed(tracked)
    offenders = [
        rel for rel in tracked if rel not in exempt and b"\x00" in (REPO / rel).read_bytes()
    ]
    assert not offenders, (
        "tracked file(s) contain a raw NUL byte, which makes git treat them as "
        "binary and hides them from review — escape the byte (e.g. `\\0`) instead, "
        f"or mark it `binary` in .gitattributes if it's a genuine binary asset: {offenders}"
    )
