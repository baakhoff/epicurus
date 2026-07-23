"""Chat, notes, and KB sinks — session gating, document routing, and refs in the ledger (#672).

The properties that matter: an **unchecked chat sink creates zero sessions** (the owner rule), a
checked one persists a reply-able session and badges it; notes/KB write through the *existing*
module document API with an ``EntityRef`` recorded on the run. File-backed SQLite per test — the
runner touches the store, and it is the automations convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import ChatMessage, EntityRef
from epicurus_core_app.agent.agent import AgentTurn, TurnUsage
from epicurus_core_app.automations.document_sinks import (
    SinkNotConfigured,
    make_document_sink,
    make_kb_sink,
    make_notes_sink,
)
from epicurus_core_app.automations.model import (
    Automation,
    AutomationRun,
    DocumentTarget,
    ScheduleTrigger,
    render_document_path,
)
from epicurus_core_app.automations.runner import AutomationRunner
from epicurus_core_app.automations.sinks import SinkDispatcher
from epicurus_core_app.automations.store import (
    AutomationQueue,
    AutomationSessionStore,
    AutomationStore,
    KillSwitchStore,
)

TENANT = "local"


# ── fakes ──────────────────────────────────────────────────────────────────────


class _FakePower:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused


class _FakeAgent:
    """Records the session_id each turn is run with — the chat-gating assertion."""

    def __init__(self, answer: str = "done") -> None:
        self.answer = answer
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        allow: frozenset[str] | None = None,
        automation_id: str | None = None,
    ) -> AgentTurn:
        self.calls.append({"session_id": session_id, "automation_id": automation_id})
        return AgentTurn(
            content=self.answer,
            stopped="completed",
            usage=TurnUsage(prompt_tokens=10, completion_tokens=5, steps=1),
        )


class _FakeWriter:
    """A DocumentWriter over an in-memory doc store — save records, get 404s when absent."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str, str], str] = {}
        self.saves: list[tuple[str, str, str, str]] = []

    async def save_page_doc(
        self, name: str, page_id: str, path: str, content: str
    ) -> dict[str, Any]:
        self.docs[(name, page_id, path)] = content
        self.saves.append((name, page_id, path, content))
        return {"path": path, "title": path, "content": content}

    async def get_page_doc(self, name: str, page_id: str, path: str) -> dict[str, Any]:
        key = (name, page_id, path)
        if key not in self.docs:
            raise RuntimeError("404 no such doc")
        return {"path": path, "content": self.docs[key]}


async def _utc() -> str:
    return "UTC"


@dataclass
class _Env:
    engine: AsyncEngine
    store: AutomationStore
    queue: AutomationQueue
    kill: KillSwitchStore
    sessions: AutomationSessionStore
    sinks: SinkDispatcher
    agent: _FakeAgent
    writer: _FakeWriter
    runner: AutomationRunner


async def _env(tmp_path: Any) -> _Env:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sinks.db'}")
    store = AutomationStore(engine)
    queue = AutomationQueue(engine)
    kill = KillSwitchStore(engine)
    sessions = AutomationSessionStore(engine)
    for s in (store, queue, kill, sessions):
        await s.init()
    sinks = SinkDispatcher()
    writer = _FakeWriter()
    sinks.register("notes", make_notes_sink(writer, _utc))
    sinks.register("kb", make_kb_sink(writer, _utc))
    agent = _FakeAgent()
    runner = AutomationRunner(
        store,
        queue,
        agent,
        _FakePower(),  # type: ignore[arg-type]  # a minimal PowerController fake
        kill,
        sinks,
        sessions=sessions,
    )
    return _Env(engine, store, queue, kill, sessions, sinks, agent, writer, runner)


def _schedule() -> ScheduleTrigger:
    return ScheduleTrigger(cadence="daily", hour=9)


# ── chat sink: session gating (the owner rule) ─────────────────────────────────


async def test_unchecked_chat_sink_creates_zero_sessions(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="Push only",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["push"],  # no chat sink
    )
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    # The run happened, but with no session — nothing to persist, no chat leaked into the list.
    assert env.agent.calls[-1]["session_id"] is None
    sid = f"automation-{automation.id}"
    assert await env.sessions.lookup(tenant=TENANT, session_ids=[sid]) == {}
    (run,) = await env.store.runs(tenant=TENANT)
    assert "chat" not in run.sinks_fired
    await env.engine.dispose()


async def test_rolling_chat_sink_persists_and_badges_one_session(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="Daily brief",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["chat"],
        chat_mode="rolling",
    )
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    session_id = f"automation-{automation.id}"
    # Both runs used the *same* rolling session, so its history accumulates and is reply-able.
    assert [c["session_id"] for c in env.agent.calls] == [session_id, session_id]
    metas = await env.sessions.lookup(tenant=TENANT, session_ids=[session_id])
    assert metas[session_id].automation_id == automation.id
    assert metas[session_id].name == "Daily brief"
    assert metas[session_id].chat_mode == "rolling"
    runs = await env.store.runs(tenant=TENANT)
    assert all("chat" in r.sinks_fired for r in runs)
    await env.engine.dispose()


async def test_per_run_chat_sink_makes_a_fresh_session_each_run(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="Weekly report",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["chat"],
        chat_mode="per_run",
    )
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    ids = [c["session_id"] for c in env.agent.calls]
    assert ids[0] != ids[1]  # a fresh session per run
    assert all(isinstance(i, str) and i.startswith(f"automation-{automation.id}-") for i in ids)
    # Both group under the same automation for the collapsible chat-list section.
    str_ids = [i for i in ids if isinstance(i, str)]
    metas = await env.sessions.lookup(tenant=TENANT, session_ids=str_ids)
    assert {m.automation_id for m in metas.values()} == {automation.id}
    assert {m.chat_mode for m in metas.values()} == {"per_run"}
    await env.engine.dispose()


async def test_silent_act_with_chat_sink_still_persists_nothing(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="Silent chore",
        prompt="do",
        autonomy="silent_act",
        schedule_trigger=_schedule(),
        sinks=["chat"],
    )
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    # silent_act fires no sinks at all — so even a checked chat sink makes no session.
    assert env.agent.calls[-1]["session_id"] is None
    sid = f"automation-{automation.id}"
    assert await env.sessions.lookup(tenant=TENANT, session_ids=[sid]) == {}
    await env.engine.dispose()


# ── notes / kb sinks: document routing + refs in the ledger ────────────────────


async def test_notes_sink_creates_a_document_and_records_the_ref(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="Report",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["notes"],
        notes_target=DocumentTarget(path_pattern="Automations/Report", mode="create"),
    )
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    # Written through the module document API, at the rendered path, with the run's output.
    assert env.writer.saves == [("notes", "notes", "Automations/Report.md", "done")]
    (run,) = await env.store.runs(tenant=TENANT)
    assert "notes" in run.sinks_fired
    assert len(run.artifacts) == 1
    assert run.artifacts[0].module == "notes"
    assert run.artifacts[0].ref_id == "Automations/Report.md"
    await env.engine.dispose()


async def test_notes_sink_append_concatenates_onto_existing(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    env.writer.docs[("notes", "notes", "Log.md")] = "# Existing\n"
    automation = await env.store.create(
        tenant=TENANT,
        name="Log",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["notes"],
        notes_target=DocumentTarget(path_pattern="Log", mode="append"),
    )
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    saved = env.writer.docs[("notes", "notes", "Log.md")]
    assert saved.startswith("# Existing")  # the prior content is kept
    assert "done" in saved  # the new entry is appended
    await env.engine.dispose()


async def test_kb_sink_writes_to_the_vault_editor(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="KB",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["kb"],
        kb_target=DocumentTarget(path_pattern="notes/summary", mode="create"),
    )
    await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    assert env.writer.saves == [("knowledge", "vault", "notes/summary.md", "done")]
    (run,) = await env.store.runs(tenant=TENANT)
    assert run.artifacts[0].module == "knowledge"
    await env.engine.dispose()


async def test_notes_sink_without_a_target_fails_gracefully(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    # A misconfigured row (the route rejects this, but the runtime must degrade, not crash).
    automation = await env.store.create(
        tenant=TENANT,
        name="Broken",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["notes"],  # no notes_target
    )
    run = await env.runner.run_once(automation, trigger_refs=[], summaries=[], verdict="schedule")
    assert run is not None
    assert run.outcome == "ok"  # the turn still succeeded; only the delivery failed
    assert "notes" not in run.sinks_fired  # recorded as failed, not fired
    assert run.artifacts == []
    assert env.writer.saves == []
    await env.engine.dispose()


# ── the document sink handler in isolation ─────────────────────────────────────


async def test_document_sink_reads_and_appends() -> None:
    writer = _FakeWriter()
    handler = make_document_sink(
        writer=writer,
        module="notes",
        page_id="notes",
        get_target=lambda a: a.notes_target,
        timezone=_utc,
    )
    automation = _automation(notes_target=DocumentTarget(path_pattern="X", mode="append"))
    ref = await handler(automation, "first")
    assert ref is not None and ref.ref_id == "X.md"
    await handler(automation, "second")
    saved = writer.docs[("notes", "notes", "X.md")]
    assert "first" in saved and "second" in saved  # accretes across runs


async def test_document_sink_missing_target_raises() -> None:
    handler = make_document_sink(
        writer=_FakeWriter(),
        module="notes",
        page_id="notes",
        get_target=lambda a: a.notes_target,
        timezone=_utc,
    )
    with pytest.raises(SinkNotConfigured):
        await handler(_automation(notes_target=None), "out")


def test_render_document_path_substitutes_date_tokens() -> None:
    now = datetime(2026, 7, 24, 9, 30, tzinfo=UTC)
    assert render_document_path("Mail report {date}", now=now) == "Mail report 2026-07-24"
    assert render_document_path("{datetime}", now=now) == "2026-07-24 09:30"
    assert render_document_path("log {time}", now=now) == "log 09:30"


# ── store round-trips ──────────────────────────────────────────────────────────


async def test_targets_round_trip_through_the_store(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    created = await env.store.create(
        tenant=TENANT,
        name="Both",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
        sinks=["notes", "kb"],
        notes_target=DocumentTarget(path_pattern="N {date}", mode="append"),
        kb_target=DocumentTarget(path_pattern="K", mode="create"),
    )
    fetched = await env.store.get(tenant=TENANT, automation_id=created.id)
    assert fetched is not None
    assert fetched.notes_target == DocumentTarget(path_pattern="N {date}", mode="append")
    assert fetched.kb_target == DocumentTarget(path_pattern="K", mode="create")
    await env.engine.dispose()


async def test_init_is_idempotent_with_ensure_columns(tmp_path: Any) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
    store = AutomationStore(engine)
    await store.init()
    await store.init()  # a second boot must be a no-op, not a duplicate-column error
    automation = await store.create(
        tenant=TENANT,
        name="X",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
    )
    assert automation.notes_target is None
    await engine.dispose()


async def test_run_artifacts_round_trip(tmp_path: Any) -> None:
    env = await _env(tmp_path)
    automation = await env.store.create(
        tenant=TENANT,
        name="X",
        prompt="do",
        autonomy="notify",
        schedule_trigger=_schedule(),
    )
    ref = EntityRef(ref_id="Doc.md", module="notes", kind="document", title="Doc")
    await env.store.record_run(
        AutomationRun(
            id="",
            tenant=TENANT,
            automation_id=automation.id,
            started_at=datetime.now(UTC),
            trigger_refs=[],
            filter_verdict="schedule",
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            duration_ms=1,
            outcome="ok",
            error=None,
            output="out",
            sinks_fired=["notes"],
            artifacts=[ref],
        )
    )
    (run,) = await env.store.runs(tenant=TENANT)
    assert run.artifacts == [ref]
    await env.engine.dispose()


async def test_session_store_record_lookup_and_delete(tmp_path: Any) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sess.db'}")
    sessions = AutomationSessionStore(engine)
    await sessions.init()
    await sessions.record(
        tenant=TENANT, session_id="s1", automation_id="a1", name="A", chat_mode="rolling"
    )
    metas = await sessions.lookup(tenant=TENANT, session_ids=["s1", "s2"])
    assert set(metas) == {"s1"}
    assert metas["s1"].name == "A"
    removed = await sessions.delete_for_automation(tenant=TENANT, automation_id="a1")
    assert removed == 1
    assert await sessions.lookup(tenant=TENANT, session_ids=["s1"]) == {}
    await engine.dispose()


def _automation(*, notes_target: DocumentTarget | None) -> Automation:
    return Automation(
        id="a1",
        tenant=TENANT,
        name="A",
        enabled=True,
        source="user",
        event_trigger=None,
        schedule_trigger=_schedule(),
        prompt="do",
        model=None,
        autonomy="notify",
        sinks=["notes"],
        chat_mode="rolling",
        chat_session_id=None,
        rate_cap_per_hour=0,
        digest_window_minutes=0,
        created_at=datetime.now(UTC),
        notes_target=notes_target,
    )
