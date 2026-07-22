"""Tests for the automations HTTP surface — CRUD, the ledger, the kill switch, templates."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import AutomationTemplate, ChatMessage
from epicurus_core_app.agent.agent import AgentTurn, TurnUsage
from epicurus_core_app.automations.routes import create_automations_router
from epicurus_core_app.automations.runner import AutomationRunner
from epicurus_core_app.automations.sinks import SinkDispatcher
from epicurus_core_app.automations.store import (
    AutomationQueue,
    AutomationStore,
    KillSwitchStore,
)

TENANT = "local"


class _FakePower:
    paused = False


class _FakeAgent:
    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        return AgentTurn(
            content="ran",
            stopped="completed",
            usage=TurnUsage(prompt_tokens=3, completion_tokens=1),
        )


async def _fresh() -> tuple[AutomationStore, KillSwitchStore, AutomationRunner, _FakeAgent]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store, queue, kill = AutomationStore(engine), AutomationQueue(engine), KillSwitchStore(engine)
    await store.init()
    await queue.init()
    await kill.init()
    agent = _FakeAgent()
    runner = AutomationRunner(
        store,
        queue,
        agent,  # type: ignore[arg-type]
        _FakePower(),  # type: ignore[arg-type]
        kill,
        SinkDispatcher(),
    )
    return store, kill, runner, agent


def _client(
    store: AutomationStore,
    kill: KillSwitchStore,
    runner: AutomationRunner,
    *,
    templates: Any = None,
) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(
        create_automations_router(store, kill, runner, templates=templates, default_tenant=TENANT)
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Tell me about pings",
        "prompt": "Say something.",
        "autonomy": "notify",
        "event_trigger": {"module": "echo", "event_type": "echo.pinged"},
        "sinks": ["chat"],
    }
    body.update(overrides)
    return body


# ── CRUD ─────────────────────────────────────────────────────────────────────


async def test_create_and_list() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        created = await client.post("/platform/v1/automations", json=_body())
        assert created.status_code == 200
        assert created.json()["name"] == "Tell me about pings"
        assert created.json()["enabled"] is True
        listed = (await client.get("/platform/v1/automations")).json()
    assert [a["name"] for a in listed] == ["Tell me about pings"]


async def test_the_view_exposes_the_derived_allowance() -> None:
    # Derived, never stored — so the UI shows the same allowance the tool surface
    # enforces rather than its own guess at what a level means.
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        notify = (await client.post("/platform/v1/automations", json=_body())).json()
        act = (await client.post("/platform/v1/automations", json=_body(autonomy="act"))).json()
    assert notify["allowed_tool_classes"] == ["read"]
    assert act["allowed_tool_classes"] == ["propose", "read", "write"]


async def test_create_rejects_a_bad_autonomy_level() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        resp = await client.post("/platform/v1/automations", json=_body(autonomy="yolo"))
    assert resp.status_code == 400
    assert "autonomy must be one of" in resp.json()["detail"]


async def test_create_rejects_no_trigger() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        resp = await client.post("/platform/v1/automations", json=_body(event_trigger=None))
    assert resp.status_code == 400
    assert "exactly one trigger" in resp.json()["detail"]


async def test_create_rejects_both_triggers() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        resp = await client.post(
            "/platform/v1/automations",
            json=_body(schedule_trigger={"cadence": "daily", "hour": 7}),
        )
    assert resp.status_code == 400


async def test_create_rejects_an_unknown_sink() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        resp = await client.post("/platform/v1/automations", json=_body(sinks=["telegram"]))
    assert resp.status_code == 400
    assert "unknown sink" in resp.json()["detail"]


async def test_a_schedule_triggered_automation_round_trips() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        created = await client.post(
            "/platform/v1/automations",
            json=_body(
                event_trigger=None, schedule_trigger={"cadence": "weekly", "hour": 9, "weekday": 2}
            ),
        )
    assert created.status_code == 200
    assert created.json()["schedule_trigger"] == {"cadence": "weekly", "hour": 9, "weekday": 2}


async def test_matchers_round_trip() -> None:
    store, kill, runner, _agent = await _fresh()
    trigger = {
        "module": "mail",
        "event_type": "mail.received",
        "matchers": [{"field": "subject", "op": "contains", "value": "lunch"}],
        "window_start_hour": 9,
        "window_end_hour": 17,
    }
    async with _client(store, kill, runner) as client:
        created = await client.post("/platform/v1/automations", json=_body(event_trigger=trigger))
    body = created.json()["event_trigger"]
    assert body["matchers"] == [{"field": "subject", "op": "contains", "value": "lunch"}]
    assert body["window_start_hour"] == 9


async def test_enable_disable_and_delete() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        created = (await client.post("/platform/v1/automations", json=_body())).json()
        toggled = await client.post(
            f"/platform/v1/automations/{created['id']}/enabled", json={"enabled": False}
        )
        assert toggled.status_code == 200
        assert (await client.get("/platform/v1/automations")).json()[0]["enabled"] is False
        deleted = await client.delete(f"/platform/v1/automations/{created['id']}")
        assert deleted.status_code == 204
        assert (await client.get("/platform/v1/automations")).json() == []


async def test_unknown_ids_are_404() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        assert (await client.delete("/platform/v1/automations/nope")).status_code == 404
        assert (
            await client.post("/platform/v1/automations/nope/enabled", json={"enabled": True})
        ).status_code == 404
        assert (await client.post("/platform/v1/automations/nope/run")).status_code == 404


# ── run now ──────────────────────────────────────────────────────────────────


async def test_run_now_runs_it_and_records_a_manual_verdict() -> None:
    store, kill, runner, agent = await _fresh()
    async with _client(store, kill, runner) as client:
        created = (await client.post("/platform/v1/automations", json=_body())).json()
        run = await client.post(f"/platform/v1/automations/{created['id']}/run")
    assert run.status_code == 200
    assert agent.calls == 1
    assert run.json()["outcome"] == "ok"
    assert run.json()["filter_verdict"] == "manual"  # distinguishable from a real trigger
    assert run.json()["output"] == "ran"


async def test_run_now_honours_the_kill_switch() -> None:
    # A test run that behaved differently from a real one would be worse than none.
    store, kill, runner, agent = await _fresh()
    async with _client(store, kill, runner) as client:
        created = (await client.post("/platform/v1/automations", json=_body())).json()
        await client.put("/platform/v1/automations/kill-switch", json={"halted": True})
        run = await client.post(f"/platform/v1/automations/{created['id']}/run")
    assert run.status_code == 409
    assert agent.calls == 0


# ── the ledger ───────────────────────────────────────────────────────────────


async def test_the_runs_feed_reports_the_ledger() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        created = (await client.post("/platform/v1/automations", json=_body())).json()
        await client.post(f"/platform/v1/automations/{created['id']}/run")
        runs = (await client.get("/platform/v1/automations/runs")).json()
    assert len(runs) == 1
    assert runs[0]["automation_id"] == created["id"]
    assert runs[0]["prompt_tokens"] == 3
    assert runs[0]["duration_ms"] is not None


async def test_the_runs_feed_filters_by_automation() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        first = (await client.post("/platform/v1/automations", json=_body())).json()
        second = (await client.post("/platform/v1/automations", json=_body(name="other"))).json()
        await client.post(f"/platform/v1/automations/{first['id']}/run")
        await client.post(f"/platform/v1/automations/{second['id']}/run")
        filtered = (
            await client.get("/platform/v1/automations/runs", params={"automation_id": first["id"]})
        ).json()
    assert [r["automation_id"] for r in filtered] == [first["id"]]


async def test_the_runs_feed_rejects_an_out_of_range_limit() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        assert (
            await client.get("/platform/v1/automations/runs", params={"limit": 0})
        ).status_code == 422
        assert (
            await client.get("/platform/v1/automations/runs", params={"limit": 9999})
        ).status_code == 422


# ── the kill switch ──────────────────────────────────────────────────────────


async def test_the_kill_switch_round_trips() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        assert (await client.get("/platform/v1/automations/kill-switch")).json() == {
            "halted": False
        }
        await client.put("/platform/v1/automations/kill-switch", json={"halted": True})
        assert (await client.get("/platform/v1/automations/kill-switch")).json() == {"halted": True}


# ── vocabulary + templates ───────────────────────────────────────────────────


async def test_the_vocabulary_is_served_so_the_ui_never_hardcodes_it() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        vocab = (await client.get("/platform/v1/automations/vocabulary")).json()
    assert vocab["autonomy_levels"] == ["notify", "propose", "act", "silent_act"]
    assert set(vocab["sinks"]) == {"push", "chat", "notes", "kb"}
    assert "contains" in vocab["matcher_ops"]


async def test_templates_are_listed_but_never_instantiated() -> None:
    # Declaring a template creates nothing: installing a module must never make the
    # assistant start doing things unasked.
    store, kill, runner, _agent = await _fresh()

    async def _templates() -> list[tuple[str, AutomationTemplate]]:
        return [
            (
                "echo",
                AutomationTemplate(
                    key="on-ping",
                    name="Tell me when pinged",
                    description="d",
                    trigger={"module": "echo", "event_type": "echo.pinged"},
                    prompt="p",
                    autonomy="notify",
                    sinks=["chat"],
                ),
            )
        ]

    async with _client(store, kill, runner, templates=_templates) as client:
        listed = (await client.get("/platform/v1/automations/templates")).json()
        live = (await client.get("/platform/v1/automations")).json()
    assert listed[0]["module"] == "echo"
    assert listed[0]["key"] == "on-ping"
    assert live == []  # nothing was created


async def test_templates_are_empty_without_a_lookup() -> None:
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        assert (await client.get("/platform/v1/automations/templates")).json() == []


async def test_an_instantiated_template_records_its_source() -> None:
    # So the Automations page can explain where a row the operator didn't hand-write
    # came from — and later edits to the module's template never reach back into it.
    store, kill, runner, _agent = await _fresh()
    async with _client(store, kill, runner) as client:
        created = await client.post("/platform/v1/automations", json=_body(source="template:echo"))
    assert created.status_code == 200
    assert created.json()["source"] == "template:echo"
