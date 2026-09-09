"""The core's own data sets (#867): what travels, and what a round trip must preserve.

Drives the real tables over a **file-backed** SQLite database (never in-memory +
StaticPool — see AGENTS.md), so the generic column reader is exercised against the actual
models rather than a stand-in: a ``LargeBinary`` attachment, a ``JSON`` trigger, a
timezone-aware ``created_at``, and a surrogate key that must *not* travel.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import PortabilityRecord
from epicurus_core_app.module_prefs import ModulePrefsStore
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


# ── a NULL a reconciled source carries (#903) ─────────────────────────────────


def _spec(kind: str) -> Any:
    return next(s for group in CORE_SETS.values() for s in group if s.kind == kind)


async def _reconciled_module_prefs(tmp_path: Path) -> AsyncEngine:
    """A source the way a long-lived install actually is: ``suggestions_enabled`` **nullable**.

    Not hand-carved DDL pretending to be old — the real path. ``module_prefs`` is rebuilt
    with only the columns of its *first* release and a row in it, then
    :meth:`ModulePrefsStore.init` runs, which is where the shared additive reconcile (#249,
    ADR-0067) adds the rest. It has nothing to backfill a populated table with, so a column
    with no ``server_default`` is added **nullable**, and the pre-existing row reads ``NULL``
    in a column the model declares ``NOT NULL``. That is the state that produced the
    ``NotNullViolationError`` in #903. Every other travelling table is created normally, so
    the whole ``prefs`` set can still be exported around it.
    """
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE module_prefs")
        await conn.exec_driver_sql(
            "CREATE TABLE module_prefs ("
            " tenant VARCHAR(63) NOT NULL,"
            " module VARCHAR(128) NOT NULL,"
            " enabled BOOLEAN NOT NULL,"
            " PRIMARY KEY (tenant, module))"
        )
        await conn.exec_driver_sql(
            "INSERT INTO module_prefs (tenant, module, enabled) VALUES ('local', 'calendar', 1)"
        )
    await ModulePrefsStore(engine).init()
    return engine


async def test_a_reconciled_source_really_does_hold_a_null_in_a_not_null_column(
    tmp_path: Path,
) -> None:
    """The premise of the whole fix, asserted rather than assumed."""
    engine = await _reconciled_module_prefs(tmp_path)
    try:
        spec = _spec("module_prefs")
        assert spec.table.c.suggestions_enabled.nullable is False  # the *model* says NOT NULL
        async with engine.connect() as conn:
            row = (await conn.execute(select(spec.table))).mappings().one()
        assert row["suggestions_enabled"] is None  # the *database* holds NULL anyway
    finally:
        await engine.dispose()


async def test_export_normalises_a_null_the_model_has_a_default_for(tmp_path: Path) -> None:
    """The archive carries the model's default, not the reconcile's NULL — portable either way."""
    engine = await _reconciled_module_prefs(tmp_path)
    try:
        record = next(r for r in await _collect(engine, "prefs") if r.kind == "module_prefs")
    finally:
        await engine.dispose()
    assert record.data["suggestions_enabled"] is True
    assert record.data["removed"] is False


async def test_import_fills_a_null_rather_than_writing_one_into_a_not_null_column(
    tmp_path: Path,
) -> None:
    """The other end: an archive written *before* the export normalised still applies.

    Asserted on the values that reach the table, not merely on the absence of an exception:
    the failure this fixes is a constraint violation, and a test that only says "no error"
    would pass just as happily against an insert that wrote ``NULL`` into a column the target
    happens to declare nullable.
    """
    engine = await _engine(tmp_path)
    spec = _spec("module_prefs")
    try:
        report = await import_set(
            engine,
            "prefs",
            _replay(
                [
                    PortabilityRecord(
                        kind="module_prefs",
                        id="calendar",
                        # Exactly the row the #903 archive carried: every post-release boolean
                        # NULL, the JSON columns backfilled by their server defaults.
                        data={
                            "module": "calendar",
                            "enabled": None,
                            "removed": None,
                            "models": "{}",
                            "disabled_tools": "[]",
                            "collections": "{}",
                            "suggestions_enabled": None,
                        },
                    )
                ]
            ),
            tenant=TENANT,
            dry_run=False,
        )
        assert report.counts["module_prefs"].created == 1
        async with engine.connect() as conn:
            row = (await conn.execute(select(spec.table))).mappings().one()
    finally:
        await engine.dispose()
    assert row["enabled"] is True
    assert row["removed"] is False
    assert row["suggestions_enabled"] is True


async def test_re_applying_a_pre_fix_archive_is_still_a_no_op(tmp_path: Path) -> None:
    """A `null` and the default it stands for are the same value, so the second apply skips.

    Without normalising *before* the comparison the archive's ``null`` would never equal the
    row's ``true``, and every re-apply of an old archive would report `updated` and rewrite
    rows it had no reason to touch.
    """
    engine = await _engine(tmp_path)
    record = PortabilityRecord(
        kind="module_prefs",
        id="calendar",
        data={"module": "calendar", "suggestions_enabled": None},
    )
    try:
        first = await import_set(engine, "prefs", _replay([record]), tenant=TENANT, dry_run=False)
        second = await import_set(engine, "prefs", _replay([record]), tenant=TENANT, dry_run=False)
    finally:
        await engine.dispose()
    assert first.counts["module_prefs"].created == 1
    assert second.counts["module_prefs"].skipped == 1
    assert second.counts["module_prefs"].updated == 0


async def test_a_null_that_cannot_be_defaulted_costs_one_row_not_the_whole_set(
    tmp_path: Path,
) -> None:
    """The #903 headline: one bad row must not take `llm_prefs`, `saved_models` and the rest.

    ``maintenance_schedule_prefs.cadence`` is ``NOT NULL`` with no default of any kind, so
    there is nothing to fill it with — the record is refused before a statement is built,
    named in a warning, and the set's other records still land. The set runs in one
    transaction, so letting the insert raise would lose every one of them.
    """
    engine = await _engine(tmp_path)
    try:
        report = await import_set(
            engine,
            "prefs",
            _replay(
                [
                    PortabilityRecord(
                        kind="maintenance_schedule_prefs",
                        id="maintenance_schedule_prefs",
                        data={"enabled": True, "cadence": None, "hour": 3, "weekday": None},
                    ),
                    PortabilityRecord(
                        kind="timezone_prefs",
                        id="timezone_prefs",
                        data={"timezone": "Europe/Berlin"},
                    ),
                    PortabilityRecord(
                        kind="module_prefs",
                        id="calendar",
                        data={"module": "calendar", "suggestions_enabled": None},
                    ),
                ]
            ),
            tenant=TENANT,
            dry_run=False,
        )
        counts = await asyncio.gather(
            _count(engine, "maintenance_schedule_prefs"),
            _count(engine, "timezone_prefs"),
            _count(engine, "module_prefs"),
        )
    finally:
        await engine.dispose()

    assert report.counts["maintenance_schedule_prefs"].skipped == 1
    assert report.counts["timezone_prefs"].created == 1
    assert report.counts["module_prefs"].created == 1
    # The warning names the column, so the operator knows what to fix rather than what failed.
    assert any("cadence" in w for w in report.warnings)
    assert list(counts) == [0, 1, 1]


def test_every_nullable_defaulted_column_in_a_travelling_table_is_covered() -> None:
    """The audit as a guard: a column added tomorrow inherits the rule without an edit here.

    Every travelling column the model marks ``NOT NULL`` while giving it only a Python-side
    default is a column the reconcile adds nullable — i.e. a future #903. The rule is derived
    from column metadata rather than a hand-kept list, so this asserts the derivation holds
    for every one of them at once.
    """
    at_risk = [
        (spec.kind, column.name)
        for group in CORE_SETS.values()
        for spec in group
        for column in spec.columns
        if not column.nullable
        and column.default is not None
        and getattr(column.default, "is_scalar", False)
    ]
    # The shapes the #903 audit found; a new one is welcome, a missing one is a regression.
    assert ("module_prefs", "suggestions_enabled") in at_risk
    assert ("automations", "autonomy") in at_risk
    assert ("push_prefs", "quiet_hours_start") in at_risk
    for kind, name in at_risk:
        spec = _spec(kind)
        normalized, undefaultable = spec.normalize({name: None})
        assert undefaultable == (), f"{kind}.{name} was not defaulted"
        assert normalized[name] is not None, f"{kind}.{name} normalised to None"


def test_a_nullable_columns_null_is_left_alone() -> None:
    """A NULL that means something is data, not a defect — defaulting it would rewrite rows."""
    spec = _spec("automations")
    normalized, undefaultable = spec.normalize({"model": None, "enabled": None})
    assert normalized["model"] is None  # nullable: "use the core default model"
    assert normalized["enabled"] is True  # NOT NULL with a default: filled
    assert undefaultable == ()
