"""The core's own data sets (#867): what travels, and what a round trip must preserve.

Drives the real tables over a **file-backed** SQLite database (never in-memory +
StaticPool — see AGENTS.md), so the generic column reader is exercised against the actual
models rather than a stand-in: a ``LargeBinary`` attachment, a ``JSON`` trigger, a
timezone-aware ``created_at``, and a surrogate key that must *not* travel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import PortabilityRecord
from epicurus_core_app.portability.core_data import (
    CORE_SETS,
    export_set,
    import_set,
)

TENANT = "local"
OTHER = "other"
WHEN = datetime(2026, 9, 4, 12, 30, 15, 123456, tzinfo=UTC)


async def _engine(tmp_path: Path) -> AsyncEngine:
    """A file-backed SQLite engine with every travelling table created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'core.db'}")
    async with engine.begin() as conn:
        for specs in CORE_SETS.values():
            for spec in specs:
                await conn.run_sync(spec.table.create, checkfirst=True)
    return engine


async def _seed(engine: AsyncEngine, tenant: str = TENANT) -> None:
    """One row in each of the shapes worth exercising."""
    specs = {spec.kind: spec for group in CORE_SETS.values() for spec in group}
    async with engine.begin() as conn:
        await conn.execute(
            insert(specs["agent_messages"].table).values(
                tenant=tenant,
                session_id="s-1",
                role="user",
                content="what's on today?",
                created_at=WHEN,
                entity_refs=None,
                attachments=None,
                activity={"steps": [{"tool": "now"}]},
            )
        )
        await conn.execute(
            insert(specs["agent_attachments"].table).values(
                att_id="att-1",
                tenant=tenant,
                kind="file",
                title="notes.txt",
                content=b"\x00\x01binary bytes\xff",
                created_at=WHEN,
            )
        )
        await conn.execute(
            insert(specs["automations"].table).values(
                id="auto-1",
                tenant=tenant,
                name="Morning brief",
                enabled=True,
                source="user",
                event_trigger={"module": "calendar", "event_type": "event.created"},
                prompt="summarise",
                autonomy="notify",
                sinks=["chat"],
                chat_mode="rolling",
                rate_cap_per_hour=0,
                digest_window_minutes=0,
                created_at=WHEN,
                agent_gated_delivery=False,
            )
        )
        await conn.execute(
            insert(specs["timezone_prefs"].table).values(tenant=tenant, timezone="Europe/Berlin")
        )
        await conn.execute(
            insert(specs["saved_models"].table).values(
                tenant=tenant, model="gpt/gpt-4o", added_at=1_800_000_000_000_000_000
            )
        )


async def _collect(engine: AsyncEngine, set_name: str, tenant: str = TENANT) -> list[Any]:
    return [r async for r in export_set(engine, set_name, tenant=tenant)]


async def _replay(records: list[PortabilityRecord]) -> AsyncIterator[PortabilityRecord]:
    for record in records:
        yield record


async def _count(engine: AsyncEngine, kind: str, tenant: str = TENANT) -> int:
    spec = next(s for group in CORE_SETS.values() for s in group if s.kind == kind)
    async with engine.connect() as conn:
        return int(
            await conn.scalar(
                select(func.count()).select_from(spec.table).where(spec.table.c.tenant == tenant)
            )
            or 0
        )


# ── what a record looks like ──────────────────────────────────────────────────


async def test_records_carry_a_stable_natural_id_and_never_a_surrogate_or_tenant(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        conversations = await _collect(engine, "conversations")
        automations = await _collect(engine, "automations")
        prefs = await _collect(engine, "prefs")
    finally:
        await engine.dispose()

    message = next(r for r in conversations if r.kind == "agent_messages")
    # The autoincrement id is an artefact of the source database's insert order, so it
    # must not be in the payload; the identity is the natural key's values.
    assert "id" not in message.data
    assert "tenant" not in message.data
    assert message.id.startswith("s-1|2026-09-04T12:30:15.123456+00:00|user")

    automation = next(r for r in automations if r.kind == "automations")
    assert "pk" not in automation.data
    assert automation.id == "auto-1"

    # A singleton prefs table has no key of its own — the tenant is the key.
    timezone = next(r for r in prefs if r.kind == "timezone_prefs")
    assert timezone.id == "timezone_prefs"
    assert timezone.data == {"timezone": "Europe/Berlin"}


async def test_binary_and_json_columns_survive_the_json_envelope(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        records = await _collect(engine, "conversations")
        attachment = next(r for r in records if r.kind == "agent_attachments")
        message = next(r for r in records if r.kind == "agent_messages")
        assert isinstance(attachment.data["content"], str)  # base64, not raw bytes
        assert message.data["activity"] == {"steps": [{"tool": "now"}]}

        # And back again, byte for byte.
        spec = next(s for s in CORE_SETS["conversations"] if s.kind == "agent_attachments")
        assert spec.decode(attachment.data)["content"] == b"\x00\x01binary bytes\xff"
    finally:
        await engine.dispose()


# ── round trip ────────────────────────────────────────────────────────────────


async def test_export_wipe_import_restores_every_set_identically(tmp_path: Path) -> None:
    """The end-to-end promise: what came out goes back in and reads the same."""
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        before = {name: await _collect(engine, name) for name in CORE_SETS}

        async with engine.begin() as conn:
            for specs in CORE_SETS.values():
                for spec in specs:
                    await conn.execute(spec.table.delete())

        for name, records in before.items():
            await import_set(engine, name, _replay(records), tenant=TENANT, dry_run=False)

        after = {name: await _collect(engine, name) for name in CORE_SETS}
    finally:
        await engine.dispose()

    assert after == before


async def test_a_second_apply_changes_nothing(tmp_path: Path) -> None:
    """Idempotency, the property that makes 'apply it again' safe advice."""
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        records = await _collect(engine, "conversations")
        async with engine.begin() as conn:
            for spec in CORE_SETS["conversations"]:
                await conn.execute(spec.table.delete())

        first = await import_set(
            engine, "conversations", _replay(records), tenant=TENANT, dry_run=False
        )
        second = await import_set(
            engine, "conversations", _replay(records), tenant=TENANT, dry_run=False
        )

        assert first.counts["agent_messages"].created == 1
        assert second.counts["agent_messages"].skipped == 1
        assert second.counts["agent_messages"].created == 0
        assert second.counts["agent_messages"].updated == 0
        assert await _count(engine, "agent_messages") == 1
        assert await _count(engine, "agent_attachments") == 1
    finally:
        await engine.dispose()


async def test_import_updates_a_changed_row_and_never_deletes_an_unmentioned_one(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        records = await _collect(engine, "prefs")
        changed = [
            PortabilityRecord(kind=r.kind, id=r.id, data={**r.data, "timezone": "Europe/Lisbon"})
            if r.kind == "timezone_prefs"
            else r
            for r in records
        ]
        # Something present here but absent from the stream must survive untouched.
        spec = next(s for s in CORE_SETS["prefs"] if s.kind == "page_order_prefs")
        async with engine.begin() as conn:
            await conn.execute(insert(spec.table).values(tenant=TENANT, order_json="[1,2]"))

        report = await import_set(engine, "prefs", _replay(changed), tenant=TENANT, dry_run=False)

        assert report.counts["timezone_prefs"].updated == 1
        assert report.counts["saved_models"].skipped == 1
        after = {r.kind: r.data for r in await _collect(engine, "prefs")}
        assert after["timezone_prefs"]["timezone"] == "Europe/Lisbon"
        assert after["page_order_prefs"]["order_json"] == "[1,2]"
    finally:
        await engine.dispose()


async def test_dry_run_counts_without_writing(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        records = await _collect(engine, "conversations")
        async with engine.begin() as conn:
            for spec in CORE_SETS["conversations"]:
                await conn.execute(spec.table.delete())

        report = await import_set(
            engine, "conversations", _replay(records), tenant=TENANT, dry_run=True
        )

        assert report.counts["agent_messages"].created == 1
        assert await _count(engine, "agent_messages") == 0
    finally:
        await engine.dispose()


async def test_an_import_lands_in_the_target_tenant_not_the_source_one(tmp_path: Path) -> None:
    """``tenant`` is context, never payload — an archive cannot write back into its origin."""
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        records = [r for r in await _collect(engine, "conversations") if r.kind == "agent_messages"]
        await import_set(engine, "conversations", _replay(records), tenant=OTHER, dry_run=False)

        assert await _count(engine, "agent_messages", TENANT) == 1
        assert await _count(engine, "agent_messages", OTHER) == 1
        # And the row that landed is the same row, under the other tenant.
        assert [r.data for r in await _collect(engine, "conversations", OTHER)] == [
            r.data for r in records
        ]
    finally:
        await engine.dispose()


async def test_a_globally_unique_id_owned_by_another_tenant_is_skipped_not_stolen(
    tmp_path: Path,
) -> None:
    """Several core ids are unique table-wide, not per tenant (``att_id``, ``automations.id``).

    Importing another tenant's copy of one must not steal the row, and must not blow the set
    up on an IntegrityError either: it is a skip, with the reason said out loud.
    """
    engine = await _engine(tmp_path)
    await _seed(engine)
    try:
        records = await _collect(engine, "conversations")
        attachments = [r for r in records if r.kind == "agent_attachments"]
        report = await import_set(
            engine, "conversations", _replay(attachments), tenant=OTHER, dry_run=False
        )

        assert report.counts["agent_attachments"].skipped == 1
        assert report.counts["agent_attachments"].created == 0
        assert any("another tenant" in w for w in report.warnings)
        assert await _count(engine, "agent_attachments", TENANT) == 1
        assert await _count(engine, "agent_attachments", OTHER) == 0
    finally:
        await engine.dispose()


async def test_an_unknown_kind_is_skipped_with_a_warning(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    try:
        report = await import_set(
            engine,
            "prefs",
            _replay([PortabilityRecord(kind="future_prefs", id="x", data={"a": 1})]),
            tenant=TENANT,
            dry_run=False,
        )
        assert report.counts["future_prefs"].skipped == 1
        assert "future_prefs" in report.warnings[0]
    finally:
        await engine.dispose()


async def test_a_field_this_schema_has_no_column_for_is_dropped_with_a_warning(
    tmp_path: Path,
) -> None:
    """A newer source's extra column is ignored, not a failure — additive on the wire."""
    engine = await _engine(tmp_path)
    try:
        report = await import_set(
            engine,
            "prefs",
            _replay(
                [
                    PortabilityRecord(
                        kind="timezone_prefs",
                        id="timezone_prefs",
                        data={"timezone": "UTC", "invented_later": True},
                    )
                ]
            ),
            tenant=TENANT,
            dry_run=False,
        )
        assert report.counts["timezone_prefs"].created == 1
        assert any("invented_later" in w for w in report.warnings)
        assert [r.data for r in await _collect(engine, "prefs")] == [{"timezone": "UTC"}]
    finally:
        await engine.dispose()
