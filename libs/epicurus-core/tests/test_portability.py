"""The module half of the portability contract (#867) — the helper's two routes.

Drives a real store over a real ASGI app: the stream a module produces must be exactly the
stream a module accepts, so the round trip here is the whole contract. The store below is
deliberately dumb (a dict) — what is under test is the transport and the rules it enforces,
not anybody's persistence.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from epicurus_core.module import EpicurusModule
from epicurus_core.portability import (
    ImportReport,
    PortabilityRecord,
    PortabilityStore,
    add_portability_routes,
    iter_ndjson_lines,
    parse_schema,
    schema_verdict,
)

TENANT = "local"


class DictStore:
    """A minimal :class:`PortabilityStore`: rows in a dict, upserted by id."""

    def __init__(self, schema: str = "widgets/2") -> None:
        self._schema = schema
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.seen_tenants: list[str] = []

    @property
    def schema(self) -> str:
        return self._schema

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        self.seen_tenants.append(tenant_id)
        for (kind, rid), data in sorted(self.rows.items()):
            yield PortabilityRecord(kind=kind, id=rid, data=data)

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        self.seen_tenants.append(tenant_id)
        report = ImportReport(schema_name=self._schema)
        async for record in records:
            if record.kind != "widget":
                report.record(record.kind, "skipped")
                report.warn(f"unknown kind {record.kind!r}")
                continue
            key = (record.kind, record.id)
            existing = self.rows.get(key)
            if existing is None:
                if not dry_run:
                    self.rows[key] = dict(record.data)
                report.record(record.kind, "created")
            elif existing == record.data:
                report.record(record.kind, "skipped")
            else:
                if not dry_run:
                    self.rows[key] = dict(record.data)
                report.record(record.kind, "updated")
        return report


def _app(store: PortabilityStore, *, schema_module: str = "widgets") -> tuple[FastAPI, DictStore]:
    module = EpicurusModule(schema_module, version="3.1.0", portable=True)
    app = FastAPI()
    add_portability_routes(app, module, store)
    return app, store  # type: ignore[return-value]


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://module")


# ── the manifest flag ─────────────────────────────────────────────────────────


async def test_portable_defaults_off_and_is_opt_in() -> None:
    assert (await EpicurusModule("plain").manifest()).portable is False
    assert (await EpicurusModule("keen", portable=True).manifest()).portable is True


# ── schema arithmetic ─────────────────────────────────────────────────────────


def test_parse_schema_and_verdicts() -> None:
    assert parse_schema("calendar/3") == ("calendar", 3)
    with pytest.raises(ValueError):
        parse_schema("calendar")

    assert schema_verdict("widgets/2", "widgets/2") == "ok"
    assert schema_verdict("widgets/1", "widgets/2") == "older"
    assert schema_verdict("widgets/3", "widgets/2") == "newer"
    assert schema_verdict("gadgets/2", "widgets/2") == "foreign"
    assert schema_verdict("nonsense", "widgets/2") == "foreign"


async def test_iter_ndjson_lines_reassembles_across_chunk_boundaries() -> None:
    """A read boundary lands mid-record and mid-character; neither may lose data."""
    payload = json.dumps({"kind": "widget", "id": "ü", "data": {"n": 1}}).encode()
    body = payload + b"\n" + payload + b"\n"

    async def chunks() -> AsyncIterator[bytes]:
        for index in range(0, len(body), 3):  # tiny, deliberately misaligned reads
            yield body[index : index + 3]

    lines = [line async for line in iter_ndjson_lines(chunks())]
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "ü"


# ── export ────────────────────────────────────────────────────────────────────


async def test_export_streams_a_header_line_then_one_record_per_line() -> None:
    store = DictStore()
    store.rows[("widget", "a")] = {"colour": "red"}
    store.rows[("widget", "b")] = {"colour": "blue"}
    app, _ = _app(store)

    async with _client(app) as client:
        response = await client.get("/export", params={"tenant_id": TENANT})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines[0] == {"schema": "widgets/2", "component_version": "3.1.0"}
    assert [line["id"] for line in lines[1:]] == ["a", "b"]
    assert lines[1]["data"] == {"colour": "red"}
    assert store.seen_tenants == [TENANT]


async def test_export_requires_a_tenant() -> None:
    app, _ = _app(DictStore())
    async with _client(app) as client:
        assert (await client.get("/export")).status_code == 400
        assert (await client.get("/export", params={"tenant_id": "NOT VALID"})).status_code == 400


# ── import ────────────────────────────────────────────────────────────────────


def _stream(schema: str, records: list[dict[str, Any]]) -> bytes:
    header = json.dumps({"schema": schema, "component_version": "3.1.0"})
    return "\n".join([header, *(json.dumps(r) for r in records)]).encode() + b"\n"


async def test_import_creates_then_skips_on_a_second_apply() -> None:
    """The idempotency guarantee: applying the same stream twice adds nothing."""
    store = DictStore()
    app, _ = _app(store)
    body = _stream("widgets/2", [{"kind": "widget", "id": "a", "data": {"colour": "red"}}])

    async with _client(app) as client:
        first = await client.post("/import", params={"tenant_id": TENANT}, content=body)
        second = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert first.json()["counts"]["widget"] == {"created": 1, "updated": 0, "skipped": 0}
    assert second.json()["counts"]["widget"] == {"created": 0, "updated": 0, "skipped": 1}
    assert len(store.rows) == 1


async def test_import_dry_run_counts_without_writing() -> None:
    store = DictStore()
    app, _ = _app(store)
    body = _stream("widgets/2", [{"kind": "widget", "id": "a", "data": {"colour": "red"}}])

    async with _client(app) as client:
        response = await client.post(
            "/import", params={"tenant_id": TENANT, "dry_run": "true"}, content=body
        )

    assert response.json()["counts"]["widget"]["created"] == 1
    assert store.rows == {}


async def test_import_updates_a_changed_record_and_never_deletes() -> None:
    store = DictStore()
    store.rows[("widget", "a")] = {"colour": "red"}
    store.rows[("widget", "kept")] = {"colour": "green"}
    app, _ = _app(store)
    body = _stream("widgets/2", [{"kind": "widget", "id": "a", "data": {"colour": "blue"}}])

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.json()["counts"]["widget"]["updated"] == 1
    assert store.rows[("widget", "a")] == {"colour": "blue"}
    # The record absent from the stream is untouched — import is additive, never a mirror.
    assert store.rows[("widget", "kept")] == {"colour": "green"}


async def test_unknown_kinds_are_skipped_with_a_warning_not_an_error() -> None:
    store = DictStore()
    app, _ = _app(store)
    body = _stream("widgets/2", [{"kind": "sprocket", "id": "x", "data": {}}])

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["sprocket"]["skipped"] == 1
    assert "sprocket" in payload["warnings"][0]


async def test_an_older_schema_imports_with_a_warning() -> None:
    store = DictStore()
    app, _ = _app(store)
    body = _stream("widgets/1", [{"kind": "widget", "id": "a", "data": {"colour": "red"}}])

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    payload = response.json()
    assert payload["counts"]["widget"]["created"] == 1
    assert any("older schema" in w for w in payload["warnings"])


@pytest.mark.parametrize("schema", ["widgets/3", "gadgets/2"])
async def test_a_newer_or_foreign_schema_is_refused_before_anything_is_written(
    schema: str,
) -> None:
    store = DictStore()
    app, _ = _app(store)
    body = _stream(schema, [{"kind": "widget", "id": "a", "data": {"colour": "red"}}])

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 409
    assert store.rows == {}


async def test_a_malformed_record_line_is_a_400_not_a_500() -> None:
    app, _ = _app(DictStore())
    body = b'{"schema":"widgets/2"}\n{not json}\n'

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 400
    assert "line 2" in response.json()["detail"]


async def test_an_empty_stream_is_a_400() -> None:
    app, _ = _app(DictStore())
    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=b"")
    assert response.status_code == 400


async def test_export_then_import_round_trips_between_two_modules() -> None:
    """The contract in one line: what one module writes, another reads."""
    source = DictStore()
    source.rows[("widget", "a")] = {"colour": "red", "size": 3}
    source.rows[("widget", "b")] = {"colour": "blue", "size": 4}
    target = DictStore()
    source_app, _ = _app(source)
    target_app, _ = _app(target)

    async with _client(source_app) as client:
        stream = (await client.get("/export", params={"tenant_id": TENANT})).content
    async with _client(target_app) as client:
        report = (
            await client.post("/import", params={"tenant_id": "other"}, content=stream)
        ).json()

    assert report["counts"]["widget"]["created"] == 2
    assert target.rows == source.rows
    assert target.seen_tenants == ["other"]
