#!/usr/bin/env python
"""Check docs/ for link rot (issue #692).

Issue #661 existed because ``docs/DEPLOYMENT.md`` was referenced from shipped,
operator-facing UI copy and a compose comment while the file doesn't exist in the
public tree (the real doc is the gitignored ``.workspace/docs/DEPLOYMENT.md``).
Nothing caught it, and a generic markdown-link checker wouldn't catch the next
one either — it never looks inside a ``.tsx`` or a compose comment. This covers,
in order of how the issue ranks them:

1. **Docs-internal links** — every relative markdown link between ``docs/`` pages
   resolves to a real file (and, if it carries a ``#anchor``, a real heading).
2. **Repo-relative ``docs/....md`` strings quoted in shipped source** — web UI
   copy, compose comments, other top-level READMEs, ``.env.example`` — resolve to
   a real file. Test fixtures commonly use plausible-looking-but-synthetic
   ``docs/a.md``-style paths (found several while writing this), so anything
   under a ``test``/``tests`` path, or named ``*.test.*``/``test_*.py``/``*_test.py``,
   is excluded — it was never meant to resolve.

Anchor slugs approximate GitHub's algorithm (lowercase, strip everything but
word characters/spaces/hyphens, spaces to hyphens) — good enough to catch a
renamed heading; it doesn't handle GitHub's disambiguation of duplicate headings
within one file.

Usage::

    uv run python scripts/check_docs_links.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"

# Tier 2 glob patterns: shipped source worth scanning for a repo-relative
# `docs/....md` string. Deliberately excludes `*.py` app/test code in general —
# see the module docstring — but *is* re-included via SOURCE_GLOBS below because
# scripts/new_module.py carries a genuine reference; the exclusion that matters
# is by path (tests), not by extension.
SOURCE_GLOBS = ("*.md", "*.tsx", "*.ts", "*.yaml", "*.yml", ".env.example", "*.py")
# Non-docs markdown files that intentionally narrate *past* fixes (mentioning a
# now-gone path as history, not as a live reference) rather than the docs/-tree
# itself, which tier 1 already covers.
SOURCE_EXCLUDE_FILES = {"CHANGELOG.md"}

MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
DOCS_PATH_RE = re.compile(r"docs/[A-Za-z0-9_/.-]+\.md")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_SLUG_STRIP_RE = re.compile(r"[^\w\s-]")
_SLUG_SPACE_RE = re.compile(r"\s")


def _git_ls_files(*pathspecs: str) -> list[Path]:
    """Every tracked file matching *pathspecs* (or all, if none given), repo-relative."""
    out = subprocess.run(
        ["git", "ls-files", "-z", *pathspecs],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [REPO_ROOT / p for p in out.decode("utf-8").split("\0") if p]


def _is_test_path(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    if {"test", "tests"} & set(rel.parts[:-1]):
        return True
    name = rel.name
    return ".test." in name or name.startswith("test_") or name.endswith("_test.py")


def slugify(heading: str) -> str:
    """Approximate GitHub's heading -> anchor slug."""
    slug = _SLUG_STRIP_RE.sub("", heading.strip().lower())
    return _SLUG_SPACE_RE.sub("-", slug)


def _anchors_in(md_file: Path) -> set[str]:
    text = md_file.read_text(encoding="utf-8")
    return {slugify(heading) for _hashes, heading in HEADING_RE.findall(text)}


def check_docs_internal_links() -> list[str]:
    """Tier 1: every relative link between docs/ pages resolves, anchors included."""
    errors = []
    # A single `*` in a plain git pathspec (no `:(glob)` magic) already matches across
    # `/`, so this covers every depth — `docs/**/*.md` looks more correct but actually
    # excludes docs/*.md files sitting directly under docs/ (verified empirically).
    for md_file in _git_ls_files("docs/*.md"):
        text = md_file.read_text(encoding="utf-8")
        for raw_target in MD_LINK_RE.findall(text):
            target = raw_target.strip().strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue  # external, or a same-page anchor — nothing cross-file to check
            path_part, _, anchor = target.partition("#")
            if not path_part or "." not in Path(path_part).name:
                # No filename extension: a GitHub-wiki page reference (docs/_Sidebar.md's
                # `[Home](Home)`), not a repo-relative file path — nothing to resolve.
                continue
            resolved = (md_file.parent / path_part).resolve()
            rel_md = md_file.relative_to(REPO_ROOT)
            if not resolved.is_file():
                errors.append(f"{rel_md}: link to '{target}' — no such file ({path_part})")
                continue
            if anchor and resolved.suffix == ".md" and slugify(anchor) not in _anchors_in(resolved):
                rel_target = resolved.relative_to(REPO_ROOT)
                errors.append(
                    f"{rel_md}: link to '{target}' — no heading in {rel_target} slugs to "
                    f"'#{slugify(anchor)}'"
                )
    return errors


def check_source_doc_references() -> list[str]:
    """Tier 2: repo-relative docs/....md strings quoted in shipped source resolve."""
    errors = []
    for f in _git_ls_files(*SOURCE_GLOBS):
        rel = f.relative_to(REPO_ROOT)
        if rel.is_relative_to("docs") or rel.name in SOURCE_EXCLUDE_FILES or _is_test_path(f):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in DOCS_PATH_RE.finditer(text):
            referenced = match.group(0)
            if not (REPO_ROOT / referenced).is_file():
                errors.append(f"{rel}: references '{referenced}', which does not exist")
    return errors


def main() -> int:
    errors = check_docs_internal_links() + check_source_doc_references()
    if errors:
        print(f"docs link-check: {len(errors)} problem(s):\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("docs link-check: every docs/ link, anchor, and source reference resolves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
