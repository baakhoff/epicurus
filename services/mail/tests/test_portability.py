"""Tests for the mail module's portability contract (#867/#874).

``MailPortability`` is deliberately the simplest possible ``PortabilityStore``: every table
the module persists (``mail_thread``, ``mail_label``, ``mail_sync``, ``mail_landing``,
``mail_category``) is a Gmail-derived cache (ADR-0096, #623/#765), so there is no
source-of-truth record kind to carry — export always yields nothing, and import treats
every record it is handed as an unrecognized kind. No database is involved anywhere in this
file; the store never touches one.

The real module declares ``portable=False`` (see ``build_module`` in ``service.py`` and
``test_manifest_declares_not_portable`` in ``test_service.py``) — the documented convention
for "nothing worth carrying" — so the core's orchestrator never actually calls these routes
today. They are tested here anyway, wired on a locally-built ``portable=True`` module exactly
like the generic contract's own tests (``epicurus_core``'s ``test_portability.py``): this file
exercises the *transport and the store*, independent of whether mail's manifest currently
advertises it, so flipping the real flag on later needs no new test coverage.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from epicurus_core import EpicurusModule, PortabilityRecord, add_portability_routes
from epicurus_mail.portability import SCHEMA, MailPortability

TENANT = "local"
OTHER_TENANT = "other-tenant"


def _app() -> tuple[FastAPI, MailPortability]:
    module = EpicurusModule("mail", version="0.21.0", portable=True)
    store = MailPortability()
    app = FastAPI()
    add_portability_routes(app, module, store)
    return app, store


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://mail")


def _records_stream(schema: str, records: list[dict[str, Any]]) -> bytes:
    header = json.dumps({"schema": schema, "component_version": "0.21.0"})
    return "\n".join([header, *(json.dumps(r) for r in records)]).encode() + b"\n"


async def _collect(records: AsyncIterator[PortabilityRecord]) -> list[PortabilityRecord]:
    return [record async for record in records]


# ── the store, called directly ─────────────────────────────────────────────────


def test_schema_is_mail_v1() -> None:
    assert MailPortability().schema == SCHEMA == "mail/1"


async def test_export_yields_nothing_for_any_tenant() -> None:
    """Mail holds no source-of-truth data — the export stream is empty for every tenant."""
    store = MailPortability()
    for tenant in (TENANT, OTHER_TENANT):
        assert await _collect(store.export(tenant_id=tenant)) == []


async def test_import_treats_every_kind_as_unrecognized() -> None:
    store = MailPortability()

    async def records() -> AsyncIterator[PortabilityRecord]:
        yield PortabilityRecord(kind="mail_landing", id="INBOX", data={"next_cursor": "abc"})
        yield PortabilityRecord(kind="mail_category", id="INBOX:primary", data={"title": "Primary"})

    report = await store.import_(tenant_id=TENANT, records=records(), dry_run=False)

    assert report.counts["mail_landing"].skipped == 1
    assert report.counts["mail_category"].skipped == 1
    assert report.total == 2
    assert any("mail_landing" in w for w in report.warnings)
    assert any("mail_category" in w for w in report.warnings)


async def test_import_dry_run_matches_a_real_apply() -> None:
    """Nothing is ever written either way, so dry_run and a real apply count identically."""
    store = MailPortability()

    def stream() -> AsyncIterator[PortabilityRecord]:
        async def _gen() -> AsyncIterator[PortabilityRecord]:
            yield PortabilityRecord(kind="mail_category", id="INBOX:primary", data={})

        return _gen()

    dry = await store.import_(tenant_id=TENANT, records=stream(), dry_run=True)
    real = await store.import_(tenant_id=TENANT, records=stream(), dry_run=False)

    assert dry.counts == real.counts
    assert dry.counts["mail_category"].skipped == 1


# ── the wired routes ─────────────────────────────────────────────────────────


async def test_export_route_streams_only_the_header_no_thread_rows() -> None:
    app, _ = _app()
    async with _client(app) as client:
        response = await client.get("/export", params={"tenant_id": TENANT})

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines == [{"schema": "mail/1", "component_version": "0.21.0"}]
    # Nothing derived from the Gmail cache (mail_thread/label/sync/landing/category) ever
    # appears on the wire.
    assert all(line.get("kind") not in {"mail_thread", "mail_label", "mail_sync"} for line in lines)


async def test_export_requires_a_tenant() -> None:
    app, _ = _app()
    async with _client(app) as client:
        assert (await client.get("/export")).status_code == 400


async def test_export_is_tenant_isolated_and_identically_empty() -> None:
    """No data exists to leak, but the route still demands a tenant and behaves the same
    (empty) way for two different ones."""
    app, _ = _app()
    async with _client(app) as client:
        first = await client.get("/export", params={"tenant_id": TENANT})
        second = await client.get("/export", params={"tenant_id": OTHER_TENANT})
    assert first.text == second.text


async def test_round_trip_export_wipe_import_is_a_clean_no_op() -> None:
    """Export -> apply into a fresh store -> the second apply is still a no-op (idempotent)."""
    app, _ = _app()
    async with _client(app) as client:
        exported = (await client.get("/export", params={"tenant_id": TENANT})).content
        first = await client.post("/import", params={"tenant_id": OTHER_TENANT}, content=exported)
        second = await client.post("/import", params={"tenant_id": OTHER_TENANT}, content=exported)

    assert first.json() == {"schema": "mail/1", "counts": {}, "warnings": []}
    assert second.json() == {"schema": "mail/1", "counts": {}, "warnings": []}


async def test_import_dry_run_via_route_counts_without_writing() -> None:
    app, _ = _app()
    body = _records_stream("mail/1", [{"kind": "mail_landing", "id": "INBOX", "data": {}}])

    async with _client(app) as client:
        response = await client.post(
            "/import", params={"tenant_id": TENANT, "dry_run": "true"}, content=body
        )

    payload = response.json()
    assert payload["counts"]["mail_landing"] == {"created": 0, "updated": 0, "skipped": 1}


async def test_unknown_kind_is_skipped_with_a_warning_not_an_error() -> None:
    app, _ = _app()
    body = _records_stream("mail/1", [{"kind": "mail_category", "id": "INBOX:primary", "data": {}}])

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["mail_category"]["skipped"] == 1
    assert "mail_category" in payload["warnings"][0]


async def test_an_older_schema_imports_with_a_warning() -> None:
    app, _ = _app()
    body = _records_stream("mail/0", [{"kind": "mail_landing", "id": "INBOX", "data": {}}])

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    payload = response.json()
    assert response.status_code == 200
    assert any("older schema" in w for w in payload["warnings"])


@pytest.mark.parametrize("schema", ["mail/2", "calendar/1"])
async def test_a_newer_or_foreign_schema_is_refused(schema: str) -> None:
    app, _ = _app()
    body = _records_stream(schema, [{"kind": "mail_landing", "id": "INBOX", "data": {}}])

    async with _client(app) as client:
        response = await client.post("/import", params={"tenant_id": TENANT}, content=body)

    assert response.status_code == 409
