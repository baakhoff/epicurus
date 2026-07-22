"""The automation runs feed (#669): replay-then-live, server-side filters, chip lookup.

Drives :class:`RunFeed` and the ``/runs`` surface against real stores on file-backed
SQLite (see ``_engine`` for why not ``:memory:``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import EntityRef, EventEnvelope
from epicurus_core_app.automations.feed import RunFeed
from epicurus_core_app.automations.model import AutomationRun
from epicurus_core_app.automations.routes import create_automations_router
from epicurus_core_app.automations.store import AutomationStore, KillSwitchStore
from epicurus_core_app.event_log import EventLogStore

TENANT = "local"


def _engine(tmp_path: Path, name: str) -> AsyncEngine:
    # File-backed, not :memory:: an in-memory aiosqlite DB exists per *connection*, and
    # the stream tests interleave a reader generator with writes — the second pooled
    # connection would see a fresh empty DB and `session.refresh` would fail with
    # "Could not refresh instance" (the same trap reported on the spine's own
    # integration test). A file is one shared DB however many connections open it.
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")


async def _store(tmp_path: Path) -> AutomationStore:
    store = AutomationStore(_engine(tmp_path, "automations.db"))
    await store.init()
    return store


def _run(
    *,
    automation_id: str = "a1",
    outcome: str = "ok",
    trigger_refs: list[int] | None = None,
    output: str = "done",
) -> AutomationRun:
    return AutomationRun(
        id=uuid.uuid4().hex,
        tenant=TENANT,
        automation_id=automation_id,
        started_at=datetime.now(UTC),
        trigger_refs=trigger_refs or [],
        filter_verdict="matched",
        model=None,
        prompt_tokens=None,
        completion_tokens=None,
        duration_ms=5,
        outcome=outcome,
        error=None,
        output=output,
        sinks_fired=[],
    )


# ── the store's outcome filter ───────────────────────────────────────────────


async def test_runs_filter_by_outcome(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.record_run(_run(outcome="ok"))
    await store.record_run(_run(outcome="skipped"))
    await store.record_run(_run(outcome="error"))

    skipped = await store.runs(tenant=TENANT, outcome="skipped")
    assert [r.outcome for r in skipped] == ["skipped"]
    everything = await store.runs(tenant=TENANT)
    assert len(everything) == 3


# ── RunFeed: replay then live ────────────────────────────────────────────────


async def test_stream_replays_history_then_yields_live(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    recorded = await store.record_run(_run(output="historic"))
    feed = RunFeed(store)

    got: list[AutomationRun] = []

    async def consume() -> None:
        async for run in feed.stream(tenant=TENANT):
            got.append(run)
            if len(got) == 2:
                break

    task = asyncio.create_task(consume())
    # Let the consumer replay history and park on the live queue.
    for _ in range(50):
        await asyncio.sleep(0)
        if got:
            break
    live = await store.record_run(_run(output="live"))
    await feed.publish(live)
    await asyncio.wait_for(task, timeout=5.0)

    assert [r.output for r in got] == ["historic", "live"]
    assert got[0].id == recorded.id


async def test_stream_applies_filters_to_live_entries(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    feed = RunFeed(store)
    got: list[AutomationRun] = []

    async def consume() -> None:
        async for run in feed.stream(tenant=TENANT, outcome="skipped"):
            got.append(run)
            break

    task = asyncio.create_task(consume())
    # The subscriber registers before the (empty) history query — wait for it so the
    # publishes below can't race the registration.
    for _ in range(200):
        if feed._subscribers:
            break
        await asyncio.sleep(0)
    await feed.publish(await store.record_run(_run(outcome="ok")))
    await feed.publish(await store.record_run(_run(outcome="skipped", output="capped")))
    await asyncio.wait_for(task, timeout=5.0)

    assert [r.output for r in got] == ["capped"]


async def test_closed_stream_unregisters_its_subscriber(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    # One historic run so the first `anext` returns from replay rather than parking
    # forever on an empty live queue (an empty-store stream only yields once published).
    await store.record_run(_run())
    feed = RunFeed(store)

    stream = feed.stream(tenant=TENANT)
    assert await anext(stream) is not None  # enter the generator; subscriber registers
    assert len(feed._subscribers) == 1
    await stream.aclose()
    assert feed._subscribers == []


# ── the HTTP surface ─────────────────────────────────────────────────────────


class _NoRunner:
    """The runner is unused by the read-side routes under test."""


async def _client(
    store: AutomationStore,
    tmp_path: Path,
    *,
    feed: RunFeed | None = None,
    events: EventLogStore | None = None,
) -> AsyncClient:
    kill = KillSwitchStore(_engine(tmp_path, "kill.db"))
    await kill.init()
    app = FastAPI()
    app.include_router(
        create_automations_router(
            store,
            kill,
            _NoRunner(),  # type: ignore[arg-type]
            default_tenant=TENANT,
            feed=feed,
            events=events,
        )
    )
    return AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    )


async def test_runs_endpoint_rejects_unknown_outcome(tmp_path: Path) -> None:
    async with await _client(await _store(tmp_path), tmp_path) as client:
        resp = await client.get("/platform/v1/automations/runs", params={"outcome": "weird"})
    assert resp.status_code == 400


async def test_runs_endpoint_filters_by_outcome(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.record_run(_run(outcome="ok"))
    await store.record_run(_run(outcome="skipped"))
    async with await _client(store, tmp_path) as client:
        resp = await client.get("/platform/v1/automations/runs", params={"outcome": "skipped"})
    assert resp.status_code == 200
    assert [r["outcome"] for r in resp.json()] == ["skipped"]


async def test_runs_endpoint_resolves_trigger_entity_refs(tmp_path: Path) -> None:
    """A ledger row's trigger_refs come back as renderable entity-ref chips (#669)."""
    store = await _store(tmp_path)
    events = EventLogStore(_engine(tmp_path, "events.db"))
    await events.init()
    logged = await events.append(
        EventEnvelope(
            tenant_id=TENANT,
            module="mail",
            type="mail.received",
            occurred_at=datetime.now(UTC),
            dedup_key="m-1",
            entity_ref=EntityRef(ref_id="m-1", module="mail", kind="message", title="Invoice"),
            payload={"message_id": "m-1"},
        )
    )
    assert logged is not None
    await store.record_run(_run(trigger_refs=[logged.id]))

    async with await _client(store, tmp_path, events=events) as client:
        resp = await client.get("/platform/v1/automations/runs")
    assert resp.status_code == 200
    [row] = resp.json()
    assert row["trigger_refs"] == [logged.id]
    [chip] = row["trigger_entity_refs"]
    assert chip["module"] == "mail"
    assert chip["title"] == "Invoice"


async def test_runs_endpoint_skips_pruned_trigger_refs(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    events = EventLogStore(_engine(tmp_path, "events.db"))
    await events.init()
    await store.record_run(_run(trigger_refs=[424242]))  # long-pruned event id
    async with await _client(store, tmp_path, events=events) as client:
        resp = await client.get("/platform/v1/automations/runs")
    [row] = resp.json()
    assert row["trigger_entity_refs"] == []


async def test_stream_endpoint_404_when_feed_unwired(tmp_path: Path) -> None:
    async with await _client(await _store(tmp_path), tmp_path) as client:
        resp = await client.get("/platform/v1/automations/runs/stream")
    assert resp.status_code == 404


async def test_stream_endpoint_rejects_unknown_outcome(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    async with await _client(store, tmp_path, feed=RunFeed(store)) as client:
        resp = await client.get("/platform/v1/automations/runs/stream", params={"outcome": "weird"})
    assert resp.status_code == 400


class _FiniteFeed:
    """A feed whose stream ends — an ASGI client can't drive an endless SSE tail
    (httpx.ASGITransport runs the app to completion; the live tail is covered by the
    RunFeed generator tests above and by the browser)."""

    def __init__(self, runs: list[AutomationRun]) -> None:
        self._runs = runs
        self.seen_filters: dict[str, str | None] = {}

    async def stream(
        self,
        *,
        tenant: str,
        automation_id: str | None = None,
        outcome: str | None = None,
    ) -> AsyncIterator[AutomationRun]:
        self.seen_filters = {"automation_id": automation_id, "outcome": outcome}
        for run in self._runs:
            yield run


async def test_stream_endpoint_frames_runs_as_sse(tmp_path: Path) -> None:
    """The SSE surface wraps each feed entry in an `automation_run` frame."""
    store = await _store(tmp_path)
    run = _run(output="framed")
    feed = _FiniteFeed([run])
    async with await _client(store, tmp_path, feed=feed) as client:  # type: ignore[arg-type]
        resp = await client.get("/platform/v1/automations/runs/stream", params={"outcome": "ok"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: automation_run" in resp.text
    assert '"framed"' in resp.text
    assert feed.seen_filters == {"automation_id": None, "outcome": "ok"}
