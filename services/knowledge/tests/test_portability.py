"""Tests for the knowledge module's export/import contract (ADR-0133, #867, #873).

``KnowledgePortability`` carries the operator-review queue — pending suggestions
(``knowledge_suggestions``) and the resolved-decision audit trail
(``knowledge_suggestion_decisions``) — the module's only source-of-truth tenant data
outside the vault itself. Everything else the module owns (``knowledge_notes``,
``knowledge_doc_index``, ``knowledge_module_docs``, ``knowledge_versions``, the Qdrant
collections) is derived and excluded; see ``docs/services/knowledge.md`` § "Portability".

File-backed SQLite under ``tmp_path`` throughout, per the shared house style for a store
touched by more than one path in a test — here that's the export half (which opens and
closes its own session) and the import half (ditto), so nothing in-memory has to survive
across them.

``libs/epicurus-core/tests/test_portability.py`` already covers the shared NDJSON
transport (``add_portability_routes``) end to end with a dummy store; the route-level test
below only exercises the schema-compatibility gate this module's own version participates
in, not the transport mechanics themselves.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import EpicurusModule, PortabilityRecord, add_portability_routes
from epicurus_knowledge.db import NoteIndex
from epicurus_knowledge.portability import (
    DECISION_KIND,
    SCHEMA,
    SUGGESTION_KIND,
    KnowledgePortability,
)
from epicurus_knowledge.suggestions import SuggestionAuditStore, SuggestionStore

TENANT = "acme"
OTHER_TENANT = "other-co"


def _engine(tmp_path: Path, name: str) -> AsyncEngine:
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")


async def _stores(
    tmp_path: Path, name: str = "kb.db"
) -> tuple[SuggestionStore, SuggestionAuditStore]:
    engine = _engine(tmp_path, name)
    suggestions = SuggestionStore(engine)
    audit = SuggestionAuditStore(engine)
    await suggestions.init()
    await audit.init()
    return suggestions, audit


async def _collect(store: KnowledgePortability, tenant: str) -> list[PortabilityRecord]:
    return [r async for r in store.export(tenant_id=tenant)]


def _stream(records: list[PortabilityRecord]) -> AsyncIterator[PortabilityRecord]:
    async def gen() -> AsyncIterator[PortabilityRecord]:
        for r in records:
            yield r

    return gen()


# ── schema ────────────────────────────────────────────────────────────────────


def test_schema_is_knowledge_v1() -> None:
    assert SCHEMA == "knowledge/1"
    assert (
        KnowledgePortability(
            SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:")),
            SuggestionAuditStore(create_async_engine("sqlite+aiosqlite:///:memory:")),
        ).schema
        == SCHEMA
    )


# ── export shape ──────────────────────────────────────────────────────────────


async def test_export_shape_carries_suggestion_and_decision_records(tmp_path: Path) -> None:
    suggestions, audit = await _stores(tmp_path)
    suggestion = await suggestions.add(
        tenant=TENANT,
        path="projects/a.md",
        operation="create",
        proposed_content="hello",
        origin="agent",
        note="a rationale",
    )
    await audit.record(
        tenant=TENANT,
        sid="d" * 32,
        path="projects/b.md",
        operation="update",
        origin="operator",
        note="",
        proposed_at=datetime.now(UTC),
        decision="approved",
        proposed_content="old",
        applied_content="new",
    )
    portability = KnowledgePortability(suggestions, audit)
    records = await _collect(portability, TENANT)

    assert [r.kind for r in records] == [SUGGESTION_KIND, DECISION_KIND]

    suggestion_record = records[0]
    assert suggestion_record.id == suggestion.sid
    assert suggestion_record.data["path"] == "projects/a.md"
    assert suggestion_record.data["operation"] == "create"
    assert suggestion_record.data["proposed_content"] == "hello"
    assert suggestion_record.data["note"] == "a rationale"
    assert "tenant" not in suggestion_record.data
    assert "id" not in suggestion_record.data  # the surrogate pk never travels
    assert "sid" not in suggestion_record.data  # travels as record.id, not duplicated

    decision_record = records[1]
    assert decision_record.id == "d" * 32
    assert decision_record.data["decision"] == "approved"
    assert decision_record.data["applied_content"] == "new"
    assert "tenant" not in decision_record.data
    assert "id" not in decision_record.data


# ── round trip ────────────────────────────────────────────────────────────────


async def test_round_trip_export_wipe_import_is_equal(tmp_path: Path) -> None:
    src_suggestions, src_audit = await _stores(tmp_path, "src.db")
    # One suggestion with a resolved decision, one without (still pending) — both shapes.
    decided = await src_suggestions.add(
        tenant=TENANT,
        path="b.md",
        operation="update",
        proposed_content="world",
        origin="operator",
        note="",
    )
    pending = await src_suggestions.add(
        tenant=TENANT,
        path="a.md",
        operation="create",
        proposed_content="hello",
        origin="agent",
        note="note1",
    )
    await src_audit.record(
        tenant=TENANT,
        sid=decided.sid,
        path="b.md",
        operation="update",
        origin="operator",
        note="",
        proposed_at=decided.created_at,
        decision="approved",
        proposed_content="world",
        applied_content="world!",
    )
    # Mirror what SuggestionReview.approve actually does (#220): the pending row is dropped
    # once its decision is recorded — "decided" now lives only in the audit trail.
    assert await src_suggestions.delete(tenant=TENANT, sid=decided.sid)

    source = KnowledgePortability(src_suggestions, src_audit)
    records = await _collect(source, TENANT)
    assert len(records) == 2  # one still-pending suggestion + one resolved decision

    dst_suggestions, dst_audit = await _stores(tmp_path, "dst.db")
    dest = KnowledgePortability(dst_suggestions, dst_audit)
    report = await dest.import_(tenant_id=TENANT, records=_stream(records), dry_run=False)
    assert report.counts[SUGGESTION_KIND].created == 1
    assert report.counts[DECISION_KIND].created == 1

    dst_pending = {s.sid: s for s in await dst_suggestions.list(tenant=TENANT)}
    assert set(dst_pending) == {pending.sid}
    assert dst_pending[pending.sid].path == "a.md"
    assert dst_pending[pending.sid].proposed_content == "hello"
    assert dst_pending[pending.sid].note == "note1"

    dst_decisions = {d.id: d for d in await dst_audit.list(tenant=TENANT, limit=10)}
    assert set(dst_decisions) == {decided.sid}
    assert dst_decisions[decided.sid].applied_content == "world!"
    assert dst_decisions[decided.sid].decision == "approved"


async def test_second_apply_of_the_same_archive_is_a_no_op(tmp_path: Path) -> None:
    src_suggestions, src_audit = await _stores(tmp_path, "src.db")
    await src_suggestions.add(
        tenant=TENANT,
        path="a.md",
        operation="create",
        proposed_content="hi",
        origin="agent",
        note="",
    )
    source = KnowledgePortability(src_suggestions, src_audit)
    records = await _collect(source, TENANT)

    dst_suggestions, dst_audit = await _stores(tmp_path, "dst.db")
    dest = KnowledgePortability(dst_suggestions, dst_audit)
    first = await dest.import_(tenant_id=TENANT, records=_stream(records), dry_run=False)
    assert first.counts[SUGGESTION_KIND].created == 1

    second = await dest.import_(tenant_id=TENANT, records=_stream(records), dry_run=False)
    assert second.counts[SUGGESTION_KIND].skipped == 1
    assert second.counts[SUGGESTION_KIND].created == 0
    assert second.counts[SUGGESTION_KIND].updated == 0
    # No duplicate row was created.
    assert len(await dst_suggestions.list(tenant=TENANT)) == 1


async def test_reapply_with_a_changed_field_reports_updated(tmp_path: Path) -> None:
    dst_suggestions, dst_audit = await _stores(tmp_path)
    dest = KnowledgePortability(dst_suggestions, dst_audit)
    record = PortabilityRecord(
        kind=SUGGESTION_KIND,
        id="fixed-sid",
        data={
            "path": "a.md",
            "operation": "create",
            "proposed_content": "v1",
            "origin": "agent",
            "note": "",
            "to_path": "",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    await dest.import_(tenant_id=TENANT, records=_stream([record]), dry_run=False)

    revised = PortabilityRecord(
        kind=SUGGESTION_KIND, id="fixed-sid", data={**record.data, "note": "v2"}
    )
    report = await dest.import_(tenant_id=TENANT, records=_stream([revised]), dry_run=False)
    assert report.counts[SUGGESTION_KIND].updated == 1
    assert report.counts[SUGGESTION_KIND].created == 0
    rows = await dst_suggestions.list(tenant=TENANT)
    assert len(rows) == 1
    assert rows[0].note == "v2"


# ── dry run ───────────────────────────────────────────────────────────────────


async def test_dry_run_counts_without_writing(tmp_path: Path) -> None:
    dst_suggestions, dst_audit = await _stores(tmp_path)
    dest = KnowledgePortability(dst_suggestions, dst_audit)
    record = PortabilityRecord(
        kind=SUGGESTION_KIND,
        id="would-be-sid",
        data={
            "path": "a.md",
            "operation": "create",
            "proposed_content": "hi",
            "origin": "agent",
            "note": "",
            "to_path": "",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    report = await dest.import_(tenant_id=TENANT, records=_stream([record]), dry_run=True)
    assert report.counts[SUGGESTION_KIND].created == 1
    assert await dst_suggestions.list(tenant=TENANT) == []  # nothing actually written


# ── unknown kind ──────────────────────────────────────────────────────────────


async def test_unknown_kind_is_skipped_with_a_warning(tmp_path: Path) -> None:
    dst_suggestions, dst_audit = await _stores(tmp_path)
    dest = KnowledgePortability(dst_suggestions, dst_audit)
    record = PortabilityRecord(kind="mystery", id="x", data={"whatever": True})
    report = await dest.import_(tenant_id=TENANT, records=_stream([record]), dry_run=False)
    assert report.counts["mystery"].skipped == 1
    assert any("mystery" in w for w in report.warnings)


# ── tenant isolation ────────────────────────────────────────────────────────


async def test_export_is_tenant_scoped(tmp_path: Path) -> None:
    suggestions, audit = await _stores(tmp_path)
    await suggestions.add(
        tenant=TENANT,
        path="mine.md",
        operation="create",
        proposed_content="x",
        origin="agent",
        note="",
    )
    await suggestions.add(
        tenant=OTHER_TENANT,
        path="theirs.md",
        operation="create",
        proposed_content="y",
        origin="agent",
        note="",
    )
    portability = KnowledgePortability(suggestions, audit)
    records = await _collect(portability, TENANT)
    assert len(records) == 1
    assert records[0].data["path"] == "mine.md"


async def test_import_writes_only_into_the_target_tenant(tmp_path: Path) -> None:
    dst_suggestions, dst_audit = await _stores(tmp_path)
    dest = KnowledgePortability(dst_suggestions, dst_audit)
    record = PortabilityRecord(
        kind=SUGGESTION_KIND,
        id="cross-tenant-sid",
        data={
            "path": "a.md",
            "operation": "create",
            "proposed_content": "hi",
            "origin": "agent",
            "note": "",
            "to_path": "",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    await dest.import_(tenant_id=TENANT, records=_stream([record]), dry_run=False)
    assert await dst_suggestions.list(tenant=TENANT) != []
    assert await dst_suggestions.list(tenant=OTHER_TENANT) == []


# ── no derived vault content or vectors travel ───────────────────────────────


async def test_export_carries_no_vault_index_content_or_vectors(tmp_path: Path) -> None:
    """`knowledge_notes` (the vault's indexed content) and Qdrant vectors are derived —
    rebuilt after import by the file rescan and re-embed fan-out (#332, #848) — and must
    never appear in this contract's stream, only the suggestion/decision queue."""
    engine = _engine(tmp_path, "kb.db")
    suggestions = SuggestionStore(engine)
    audit = SuggestionAuditStore(engine)
    await suggestions.init()
    await audit.init()
    notes = NoteIndex(engine)
    await notes.init()
    # A distinctive marker in the vault's *index*, not the export's business at all.
    await notes.upsert(
        tenant=TENANT,
        note_path="private/secret-vault-note.md",
        mtime_ns=123,
        content_hash="deadbeefcafe",
        chunk_count=3,
    )
    await suggestions.add(
        tenant=TENANT,
        path="a.md",
        operation="create",
        proposed_content="an ordinary suggestion body",
        origin="agent",
        note="",
    )

    portability = KnowledgePortability(suggestions, audit)
    records = await _collect(portability, TENANT)
    serialized = json.dumps([r.model_dump() for r in records])

    assert "secret-vault-note.md" not in serialized
    assert "deadbeefcafe" not in serialized
    for record in records:
        assert "vector" not in record.data
        assert "embedding" not in record.data
        assert record.kind in (SUGGESTION_KIND, DECISION_KIND)


# ── the route-level schema gate ───────────────────────────────────────────────


async def test_route_rejects_a_newer_schema_and_warns_on_an_older_one(tmp_path: Path) -> None:
    """The shared transport's compatibility rule (`epicurus_core.schema_verdict`), exercised
    with *this* module's own schema string — the transport mechanics themselves are covered
    once, generically, in `libs/epicurus-core/tests/test_portability.py`."""
    suggestions, audit = await _stores(tmp_path)
    portability = KnowledgePortability(suggestions, audit)
    module = EpicurusModule("knowledge", version="0.30.0", portable=True)
    app = FastAPI()
    add_portability_routes(app, module, portability)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://module") as client:
        newer = json.dumps({"schema": "knowledge/2", "component_version": "9.9.9"}) + "\n"
        resp = await client.post("/import", params={"tenant_id": TENANT}, content=newer)
        assert resp.status_code == 409

        older_header = json.dumps({"schema": "knowledge/0", "component_version": "0.1.0"})
        record = json.dumps(
            {
                "kind": SUGGESTION_KIND,
                "id": "older-sid",
                "data": {
                    "path": "x.md",
                    "operation": "create",
                    "proposed_content": "",
                    "origin": "agent",
                    "note": "",
                    "to_path": "",
                    "created_at": "2020-01-01T00:00:00+00:00",
                },
            }
        )
        body = (older_header + "\n" + record + "\n").encode()
        resp2 = await client.post("/import", params={"tenant_id": TENANT}, content=body)
        assert resp2.status_code == 200
        payload = resp2.json()
        assert any("older" in w for w in payload["warnings"])
        assert payload["counts"][SUGGESTION_KIND]["created"] == 1
