"""nginx must never refuse an upload the core would have accepted (#891, the #888 trap).

Two independent ceilings sit on the one upload path: nginx's ``client_max_body_size`` on the
``/platform/`` proxy block, and the core's own ``ATTACHMENT_MAX_BYTES`` (default
``upload_limits.DEFAULT_MAX_UPLOAD_BYTES``). They live in different files, in different
languages, maintained by different changes — and if the edge one is the smaller, a legitimate
upload dies at the proxy with nginx's HTML 413 instead of reaching the core's clean JSON error
with its content-type allowlist. Raising the core's default without raising the template is a
one-line change that nothing else in the repo notices: `compose-validate` lints YAML, the unit
suites never see nginx, and the smoke gate uploads nothing near the limit.

So: parse the template the image actually renders, and assert the edge cap is at least the
core's default. The archive-import location (#887) is exempt by design — it sets
``client_max_body_size 0`` so the core's own 4 GiB ceiling is the only one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from epicurus_core_app.upload_limits import DEFAULT_MAX_UPLOAD_BYTES

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "services" / "web" / "nginx.conf.template"

_SUFFIXES = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _parse_size(value: str) -> int:
    """nginx's own size grammar: a number with an optional ``k``/``m``/``g`` suffix."""
    match = re.fullmatch(r"(\d+)([kmgKMG]?)", value.strip())
    assert match, f"not an nginx size: {value!r}"
    return int(match.group(1)) * _SUFFIXES[match.group(2).lower()]


def _location_block(name: str) -> str:
    """The body of ``location <name> { … }`` from the template (blocks here don't nest)."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index(f"location {name} {{")
    end = text.index("\n    }", start)
    return text[start:end]


def test_platform_upload_cap_is_at_least_the_core_default() -> None:
    block = _location_block("/platform/")
    match = re.search(r"client_max_body_size\s+([^;]+);", block)
    assert match, "the /platform/ proxy block no longer caps upload bodies at all"
    assert _parse_size(match.group(1)) >= DEFAULT_MAX_UPLOAD_BYTES, (
        "nginx's client_max_body_size on /platform/ is below the core's "
        f"DEFAULT_MAX_UPLOAD_BYTES ({DEFAULT_MAX_UPLOAD_BYTES} bytes) — a legitimate upload "
        "would be refused at the edge with an HTML 413 instead of reaching the core"
    )


def test_the_archive_import_location_stays_uncapped() -> None:
    """#887: the one upload that is legitimately huge streams through, capped only by the core."""
    block = _location_block("= /platform/v1/portability/imports")
    assert re.search(r"client_max_body_size\s+0;", block), (
        "the archive-import location must keep `client_max_body_size 0` so the core's own "
        "PORTABILITY_MAX_ARCHIVE_MB ceiling is the only one"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12m", 12 * 1024**2), ("0", 0), ("512k", 512 * 1024), ("1G", 1024**3)],
)
def test_size_parser_understands_nginx_sizes(text: str, expected: int) -> None:
    # Guard against the parser silently mis-reading a suffix and making the check vacuous.
    assert _parse_size(text) == expected
