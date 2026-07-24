"""``scripts/check_docs_links.py`` — no dead docs/ references reach an operator (#692).

Real-repo tests assert the actual ``docs/`` tree is clean (the fast-gate mirror of
the CI job) and guard against a vacuously-passing regex. Synthetic-repo tests (a
throwaway ``git init``, so ``git ls-files`` has something to list) exercise each
failure mode directly, including the exact false-positive class found while
writing the checker: test fixtures commonly use plausible-looking-but-synthetic
``docs/a.md``-style paths that were never meant to resolve.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def _point_at(check_docs_links: ModuleType, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(check_docs_links, "REPO_ROOT", root)
    monkeypatch.setattr(check_docs_links, "DOCS_ROOT", root / "docs")


# ── the real repo ──────────────────────────────────────────────────────────────


def test_the_real_docs_tree_has_no_broken_links(check_docs_links: ModuleType) -> None:
    errors = check_docs_links.check_docs_internal_links()
    assert errors == []


def test_the_real_source_tree_has_no_broken_doc_references(check_docs_links: ModuleType) -> None:
    errors = check_docs_links.check_source_doc_references()
    assert errors == []


def test_docs_path_regex_is_not_vacuous(check_docs_links: ModuleType) -> None:
    """Guard against the tier-2 regex silently matching nothing (scripts/new_module.py
    references docs/reference/ports.md in a comment and an error message)."""
    text = (REPO / "scripts" / "new_module.py").read_text(encoding="utf-8")
    assert check_docs_links.DOCS_PATH_RE.search(text) is not None


def test_md_link_regex_is_not_vacuous(check_docs_links: ModuleType) -> None:
    text = (REPO / "docs" / "index.md").read_text(encoding="utf-8")
    assert check_docs_links.MD_LINK_RE.search(text) is not None


# ── slugify (GitHub's heading -> anchor algorithm) ──────────────────────────────


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Simple heading", "simple-heading"),
        ("Configuration", "configuration"),
        # A bug in an earlier version of this checker collapsed the two spaces an
        # em-dash leaves behind (once it's stripped) into a single hyphen — GitHub's
        # real slugger keeps one hyphen per space, i.e. a double hyphen here.
        (
            "`WritesDocument` — opt a tool into the live document pane (#541, ADR-0100)",
            "writesdocument--opt-a-tool-into-the-live-document-pane-541-adr-0100",
        ),
        # A parenthetical issue/ADR suffix: parens vanish, the number stays.
        ("Docker-socket access opt-in (#622)", "docker-socket-access-opt-in-622"),
        ("Raw events feed (ADR-0103)", "raw-events-feed-adr-0103"),
    ],
)
def test_slugify_matches_github(check_docs_links: ModuleType, heading: str, expected: str) -> None:
    assert check_docs_links.slugify(heading) == expected


# ── synthetic repos: each failure mode, isolated ────────────────────────────────


def test_detects_a_broken_relative_link(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("See [b](b.md) and [gone](missing.md).\n", encoding="utf-8")
    (docs / "b.md").write_text("# B\n", encoding="utf-8")
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    errors = check_docs_links.check_docs_internal_links()

    assert len(errors) == 1
    assert "missing.md" in errors[0]


def test_detects_a_broken_anchor(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text(
        "See [good](b.md#real-heading) and [bad](b.md#renamed-heading).\n", encoding="utf-8"
    )
    (docs / "b.md").write_text("## Real heading\n", encoding="utf-8")
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    errors = check_docs_links.check_docs_internal_links()

    assert len(errors) == 1
    assert "renamed-heading" in errors[0]


def test_ignores_wiki_style_bare_page_references(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`docs/_Sidebar.md`'s `[Home](Home)` is a GitHub-wiki page reference, not a repo
    file path — a bare target with no extension has nothing to resolve against."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "_Sidebar.md").write_text("[Home](Home)\n", encoding="utf-8")
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    assert check_docs_links.check_docs_internal_links() == []


def test_external_and_same_page_anchor_links_are_not_checked(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text(
        "[external](https://example.com/gone.md) and [same-page](#nowhere).\n",
        encoding="utf-8",
    )
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    assert check_docs_links.check_docs_internal_links() == []


def test_detects_a_broken_source_reference(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "docs").mkdir()
    web = tmp_path / "services" / "web" / "src"
    web.mkdir(parents=True)
    (web / "ModulesScreen.tsx").write_text('const href = "docs/DEPLOYMENT.md";\n', encoding="utf-8")
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    errors = check_docs_links.check_source_doc_references()

    assert len(errors) == 1
    assert "docs/DEPLOYMENT.md" in errors[0]
    assert "ModulesScreen.tsx" in errors[0]


def test_ignores_synthetic_paths_in_test_fixtures(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact false positive found writing this checker: a `.test.tsx` mocking a
    file move with a plausible-looking `docs/a.md` that isn't a real docs reference."""
    (tmp_path / "docs").mkdir()
    web_test = tmp_path / "services" / "web" / "src" / "test"
    web_test.mkdir(parents=True)
    (web_test / "BrowserView.test.tsx").write_text(
        'move: vi.fn().mockResolvedValue({ path: "docs/a.md" }),\n', encoding="utf-8"
    )
    py_tests = tmp_path / "services" / "knowledge" / "tests"
    py_tests.mkdir(parents=True)
    (py_tests / "test_service.py").write_text(
        'await store.write_text(path="docs/a.md", content="x")\n', encoding="utf-8"
    )
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    assert check_docs_links.check_source_doc_references() == []


def test_ignores_changelog(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHANGELOG.md narrates *past* fixes — mentioning a now-gone path as history is
    not a live reference (docs/DEPLOYMENT.md's own entry is exactly this shape)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "CHANGELOG.md").write_text(
        "- Retargeted away from docs/DEPLOYMENT.md, which doesn't exist.\n", encoding="utf-8"
    )
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    assert check_docs_links.check_source_doc_references() == []


def test_ignores_docs_tree_itself(
    check_docs_links: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1 (check_docs_internal_links) owns docs/-internal link integrity; tier 2
    would otherwise double-report the same broken path."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("Mentions docs/gone.md in prose.\n", encoding="utf-8")
    _init_repo(tmp_path)
    _point_at(check_docs_links, monkeypatch, tmp_path)

    assert check_docs_links.check_source_doc_references() == []
