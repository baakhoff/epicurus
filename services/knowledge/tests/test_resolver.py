"""Tests for the hover-card resolver (#143): vault notes, platform docs, failure modes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from epicurus_knowledge.reader import DiskVaultReader
from epicurus_knowledge.refs import SOURCE_DOC, SOURCE_NOTE, encode_ref
from epicurus_knowledge.resolver import KnowledgeResolver, create_resolver_router


class _FakeLedger:
    """Stands in for NoteIndex/DocIndex — returns a fixed per-path index time."""

    def __init__(self, when: str | None = "2026-06-14T12:34:56+00:00") -> None:
        self._when = when

    async def indexed_at(self, *, tenant: str, note_path: str) -> str | None:
        return self._when


def _resolver(
    tmp_path: Path,
    note_ledger: object | None = None,
    doc_ledger: object | None = None,
) -> KnowledgeResolver:
    vault = tmp_path / "vault"
    docs = tmp_path / "docs"
    vault.mkdir()
    docs.mkdir()
    (vault / "note.md").write_text(
        "---\ntags: [alpha, beta]\n---\n# Note\n\nThe body of the note.", encoding="utf-8"
    )
    (docs / "services").mkdir()
    (docs / "services" / "knowledge.md").write_text(
        "# Knowledge service\n\nDocs body here.", encoding="utf-8"
    )
    return KnowledgeResolver(
        vault_reader=DiskVaultReader(vault),
        docs_reader=DiskVaultReader(docs),
        note_index=note_ledger or _FakeLedger(),  # type: ignore[arg-type]
        doc_index=doc_ledger or _FakeLedger(),  # type: ignore[arg-type]
        tenant="t1",
    )


async def test_resolve_vault_note_has_open_link(tmp_path: Path) -> None:
    card = await _resolver(tmp_path).resolve("knowledge", encode_ref(SOURCE_NOTE, "note.md"))
    assert card.title == "note"
    assert "body of the note" in card.description
    labels = {d.label: d.value for d in card.details}
    assert labels["Path"] == "note.md"
    assert labels["Tags"] == "alpha, beta"
    assert labels["Last indexed"] == "2026-06-14 12:34"
    assert card.href is not None
    assert card.href.url == "/m/knowledge/vault?doc=note.md"
    assert card.href.label == "Open in Knowledge"


async def test_resolve_platform_doc_has_no_link_and_docs_prefix(tmp_path: Path) -> None:
    card = await _resolver(tmp_path).resolve(
        "knowledge", encode_ref(SOURCE_DOC, "services/knowledge.md")
    )
    assert card.title == "knowledge"
    labels = {d.label: d.value for d in card.details}
    assert labels["Path"] == "docs/services/knowledge.md"
    assert card.href is None  # platform docs aren't editable in the Knowledge page


async def test_resolve_unknown_kind_is_404(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as err:
        await _resolver(tmp_path).resolve("bogus", encode_ref(SOURCE_NOTE, "note.md"))
    assert err.value.status_code == 404


async def test_resolve_missing_doc_is_404(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as err:
        await _resolver(tmp_path).resolve("knowledge", encode_ref(SOURCE_NOTE, "ghost.md"))
    assert err.value.status_code == 404


async def test_resolve_traversal_is_400(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as err:
        await _resolver(tmp_path).resolve("knowledge", encode_ref(SOURCE_NOTE, "../escape.md"))
    assert err.value.status_code == 400


async def test_resolve_survives_ledger_failure(tmp_path: Path) -> None:
    class _BoomLedger:
        async def indexed_at(self, *, tenant: str, note_path: str) -> str | None:
            raise RuntimeError("db down")

    card = await _resolver(tmp_path, note_ledger=_BoomLedger()).resolve(
        "knowledge", encode_ref(SOURCE_NOTE, "note.md")
    )
    # The card still renders; it just omits the "Last indexed" row.
    assert all(d.label != "Last indexed" for d in card.details)


async def test_resolve_note_without_frontmatter_omits_tags(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    (tmp_path / "vault" / "plain.md").write_text("# Plain\n\nNo frontmatter.", encoding="utf-8")
    card = await resolver.resolve("knowledge", encode_ref(SOURCE_NOTE, "plain.md"))
    assert all(d.label != "Tags" for d in card.details)


# ── router (the HTTP surface the core proxies) ────────────────────────────────


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_resolver_router(_resolver(tmp_path)))
    return TestClient(app)


def test_router_resolves_a_note(tmp_path: Path) -> None:
    ref = encode_ref(SOURCE_NOTE, "note.md")
    resp = _client(tmp_path).get(f"/resolve/knowledge/{ref}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "note"
    assert body["href"]["url"] == "/m/knowledge/vault?doc=note.md"


def test_router_unknown_kind_is_404(tmp_path: Path) -> None:
    ref = encode_ref(SOURCE_NOTE, "note.md")
    assert _client(tmp_path).get(f"/resolve/event/{ref}").status_code == 404
