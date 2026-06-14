"""Tests for the document-ref codec and the shared path-safety boundary (refs.py)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import HTTPException

from epicurus_knowledge.refs import (
    SOURCE_DOC,
    SOURCE_NOTE,
    decode_ref,
    doc_title,
    encode_ref,
    iter_md_files,
    safe_relative,
)


@pytest.mark.parametrize(
    ("source", "path"),
    [
        (SOURCE_NOTE, "alpha.md"),
        (SOURCE_NOTE, "projects/deep/a note with spaces.md"),
        (SOURCE_DOC, "services/knowledge.md"),
        (SOURCE_NOTE, "docs/looks-like-docs-but-vault.md"),  # prefix collision is a non-issue
    ],
)
def test_encode_decode_round_trip(source: str, path: str) -> None:
    ref = encode_ref(source, path)
    assert "/" not in ref  # slash-free → survives a single URL path segment
    assert "=" not in ref  # padding stripped
    assert decode_ref(ref) == (source, path)


def test_decode_rejects_non_base64() -> None:
    with pytest.raises(HTTPException) as err:
        decode_ref("%%% not base64 %%%")
    assert err.value.status_code == 404


def test_decode_rejects_unknown_source() -> None:
    bad = base64.urlsafe_b64encode(b"bogus:alpha.md").decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as err:
        decode_ref(bad)
    assert err.value.status_code == 404


def test_decode_rejects_missing_path() -> None:
    bad = base64.urlsafe_b64encode(b"note:").decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as err:
        decode_ref(bad)
    assert err.value.status_code == 404


def test_doc_title_strips_dirs_and_suffix() -> None:
    assert doc_title("projects/beta.md") == "beta"


def test_iter_md_files_sorted_md_only(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.md").write_text("c", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("x", encoding="utf-8")
    assert iter_md_files(tmp_path) == ["a.md", "b.md", "sub/c.md"]


def test_iter_md_files_absent_root(tmp_path: Path) -> None:
    assert iter_md_files(tmp_path / "nope") == []


@pytest.mark.parametrize(
    "bad",
    ["../escape.md", "/etc/passwd", "..\\windows\\evil.md", "notes.txt", "   "],
)
def test_safe_relative_rejects(tmp_path: Path, bad: str) -> None:
    with pytest.raises(HTTPException) as err:
        safe_relative(tmp_path, bad)
    assert err.value.status_code == 400


def test_safe_relative_confines_under_root(tmp_path: Path) -> None:
    target = safe_relative(tmp_path, "sub/note.md")
    assert target == (tmp_path.resolve() / "sub" / "note.md")
