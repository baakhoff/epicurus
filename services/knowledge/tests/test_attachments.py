"""Tests for knowledge as a chat attachment source (#137): picker + resolve."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from epicurus_knowledge.attachments import VaultAttachments, create_attachments_router
from epicurus_knowledge.refs import SOURCE_DOC, SOURCE_NOTE, decode_ref, encode_ref


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text("# Alpha\nbody", encoding="utf-8")
    sub = tmp_path / "projects"
    sub.mkdir()
    (sub / "beta.md").write_text("# Beta\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("ignored", encoding="utf-8")  # non-md is skipped
    return tmp_path


def test_list_returns_vault_docs_as_items(tmp_path: Path) -> None:
    items = VaultAttachments(_vault(tmp_path)).list()
    assert [i.title for i in items] == ["alpha", "beta"]
    assert all(i.kind == "knowledge" for i in items)
    # ref_id is opaque but decodes back to the (source, path) it names.
    assert [decode_ref(i.ref_id) for i in items] == [
        (SOURCE_NOTE, "alpha.md"),
        (SOURCE_NOTE, "projects/beta.md"),
    ]


def test_list_empty_when_no_vault(tmp_path: Path) -> None:
    assert VaultAttachments(tmp_path / "absent").list() == []


def test_read_returns_document_text(tmp_path: Path) -> None:
    att = VaultAttachments(_vault(tmp_path))
    content = att.read(encode_ref(SOURCE_NOTE, "alpha.md"))
    assert content.text == "# Alpha\nbody"
    assert content.title == "alpha"
    assert content.path == "alpha.md"


def test_read_rejects_non_vault_source(tmp_path: Path) -> None:
    # A doc-source id never came from this (vault-only) picker.
    att = VaultAttachments(_vault(tmp_path))
    with pytest.raises(HTTPException) as err:
        att.read(encode_ref(SOURCE_DOC, "alpha.md"))
    assert err.value.status_code == 404


def test_read_missing_is_404(tmp_path: Path) -> None:
    att = VaultAttachments(_vault(tmp_path))
    with pytest.raises(HTTPException) as err:
        att.read(encode_ref(SOURCE_NOTE, "ghost.md"))
    assert err.value.status_code == 404


def test_read_traversal_is_400(tmp_path: Path) -> None:
    att = VaultAttachments(_vault(tmp_path))
    with pytest.raises(HTTPException) as err:
        att.read(encode_ref(SOURCE_NOTE, "../outside.md"))
    assert err.value.status_code == 400


# ── router (the HTTP surface the core proxies) ────────────────────────────────


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_attachments_router(VaultAttachments(_vault(tmp_path))))
    return TestClient(app)


def test_router_lists_attachments(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/attachments")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["title"] for i in body] == ["alpha", "beta"]
    assert body[0]["kind"] == "knowledge"


def test_router_resolves_attachment(tmp_path: Path) -> None:
    ref = encode_ref(SOURCE_NOTE, "alpha.md")
    resp = _client(tmp_path).get(f"/attachments/{ref}")
    assert resp.status_code == 200
    assert resp.json()["text"] == "# Alpha\nbody"


def test_router_resolve_unknown_ref_is_404(tmp_path: Path) -> None:
    # Valid base64url, but the decoded payload is not a known reference.
    bad = base64.urlsafe_b64encode(b"no-colon-here").decode("ascii").rstrip("=")
    resp = _client(tmp_path).get(f"/attachments/{bad}")
    assert resp.status_code == 404
