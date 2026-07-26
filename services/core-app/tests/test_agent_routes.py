"""Tests for the agent HTTP surface — the readiness-led chat stream (ADR-0027).

The agent loop, readiness probe, memory, and attachment store are all faked; these
tests assert the route's SSE choreography, not model behavior.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent import routes as agent_routes
from epicurus_core_app.agent.agent import AgentEvent, AgentTurn
from epicurus_core_app.agent.live_runs import LiveRun, LiveRunRegistry
from epicurus_core_app.agent.pending_drafts import PendingDraftStore
from epicurus_core_app.agent.routes import create_agent_router
from epicurus_core_app.agent.session_model import SessionModelStore
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.llm.models import PowerState
from epicurus_core_app.memory.memory import MemoryItem
from epicurus_core_app.memory.profile import StandingProfileStore
from epicurus_core_app.memory.store import SessionSummary
from epicurus_core_app.readiness import Readiness, ReadinessComponent, create_readiness_router


class _FakeAgent:
    """Streams a fixed one-token turn — stands in for the real loop."""

    async def run_stream(
        self,
        messages: object,
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        persist_input: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="delta", text="hi")
        yield AgentEvent(type="done", turn=AgentTurn(content="hi", stopped="completed"))


def _pending() -> Readiness:
    return Readiness(
        ready=False,
        power=PowerState.IDLE,
        components=[ReadinessComponent(name="model", ready=False, detail="checking…")],
    )


def _resolved() -> Readiness:
    return Readiness(
        ready=True,
        power=PowerState.IDLE,
        components=[ReadinessComponent(name="model", ready=True, detail="llama3.2 · warm")],
    )


class _FakeProbe:
    """Yields a pending then resolved snapshot, with an optional pre-resolve delay."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self._delay = delay

    async def stream(
        self, *, model: str | None = None, tenant_id: str | None = None
    ) -> AsyncIterator[Readiness]:
        yield _pending()
        if self._delay:
            await asyncio.sleep(self._delay)
        yield _resolved()

    async def check(self, *, model: str | None = None, tenant_id: str | None = None) -> Readiness:
        return _resolved()


def _app(probe: object | None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_agent_router(
            _FakeAgent(),  # type: ignore[arg-type]
            object(),  # memory — unused by the routes under test  # type: ignore[arg-type]
            "local",
            object(),  # attachment store — unused  # type: ignore[arg-type]
            probe=probe,  # type: ignore[arg-type]
        )
    )
    if probe is not None:
        app.include_router(create_readiness_router(probe))  # type: ignore[arg-type]
    return app


def _parse_sse(text: str) -> list[tuple[str, dict[str, object]]]:
    frames: list[tuple[str, dict[str, object]]] = []
    for block in text.split("\n\n"):
        event = "message"
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if data:
            frames.append((event, json.loads(data)))
    return frames


async def _post_stream(app: FastAPI) -> list[tuple[str, dict[str, object]]]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/agent/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        return _parse_sse(resp.text)


async def test_chat_stream_leads_with_readiness_then_the_turn() -> None:
    frames = await _post_stream(_app(_FakeProbe()))
    types = [event for event, _ in frames]
    # Readiness leads (pending → resolved), then the turn streams.
    assert types == ["readiness", "readiness", "delta", "done"]
    assert frames[0][1]["readiness"]["ready"] is False  # pending
    assert frames[1][1]["readiness"]["ready"] is True  # resolved
    # Every readiness frame precedes the first content delta.
    assert types.index("delta") > max(i for i, t in enumerate(types) if t == "readiness")


async def test_chat_stream_without_probe_emits_no_readiness() -> None:
    frames = await _post_stream(_app(None))
    assert [event for event, _ in frames] == ["delta", "done"]


async def test_slow_readiness_probe_does_not_delay_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A probe slower than the budget is abandoned; the pending frame still shows and the
    # turn streams regardless (the resolved frame is dropped).
    monkeypatch.setattr(agent_routes, "READINESS_BUDGET_S", 0.01)
    frames = await _post_stream(_app(_FakeProbe(delay=0.2)))
    types = [event for event, _ in frames]
    assert types == ["readiness", "delta", "done"]
    assert frames[0][1]["readiness"]["ready"] is False


async def test_readiness_endpoint_returns_a_snapshot() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_FakeProbe())), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/readiness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["power"] == "idle"
    assert body["components"][0]["name"] == "model"


# ── memory view routes ──────────────────────────────────────────────────────


class _FakeMemory:
    """Stands in for the memory facade — records what the memory routes asked of it."""

    def __init__(
        self,
        *,
        last_user: int | None = 1,
        roles: dict[int, str] | None = None,
        session_list: list[SessionSummary] | None = None,
    ) -> None:
        self.searched: list[str] = []
        self.forgotten: list[str] = []
        self._last_user = last_user
        # The session's messages by id — a lookup miss stands for "not in this conversation"
        # (the real scoping is SQL, covered in test_memory_store.py).
        self._roles = roles or {}
        self.truncated_after: list[int] = []
        self.revised: list[tuple[int, str]] = []
        self._session_list = session_list or []

    async def sessions(self, *, tenant: str) -> list[SessionSummary]:
        return self._session_list

    async def last_user_message_id(self, *, tenant: str, session_id: str) -> int | None:
        return self._last_user

    async def message_role(self, *, tenant: str, session_id: str, message_id: int) -> str | None:
        return self._roles.get(message_id)

    async def truncate_after(self, *, tenant: str, session_id: str, after_id: int) -> int:
        self.truncated_after.append(after_id)
        return 1

    async def revise_message(
        self, *, tenant: str, session_id: str, message_id: int, content: str
    ) -> None:
        self.revised.append((message_id, content))

    async def memories(self, *, tenant: str, limit: int = 200) -> tuple[list[MemoryItem], int]:
        return [
            MemoryItem(id="f2", text="Lives in Belgrade", source="auto"),
            MemoryItem(id="f1", text="Prefers metric units", source="tool"),
        ], 2

    async def search_memory(
        self, *, tenant: str, query: str, limit: int = 20
    ) -> tuple[list[MemoryItem], int]:
        self.searched.append(query)
        return [MemoryItem(id="f1", text="Prefers metric units", source="tool", score=0.9)], 2

    async def forget_memory(self, *, tenant: str, memory_id: str) -> int:
        self.forgotten.append(memory_id)
        return 1


def _memory_app(
    memory: object,
    *,
    live_runs: LiveRunRegistry | None = None,
    session_models: SessionModelStore | None = None,
    agent: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_agent_router(
            agent or _FakeAgent(),  # type: ignore[arg-type]
            memory,  # type: ignore[arg-type]
            "local",
            object(),  # attachment store — unused by the memory routes  # type: ignore[arg-type]
            # Unset → the router builds a private, empty registry, so the edit route's
            # active-run guard sees no live turn (what every non-#552 test here wants).
            live_runs=live_runs,
            session_models=session_models,
        )
    )
    return app


# ── sessions list + per-session model override (#707) ────────────────────────


def _session(id_: str, *, model: str | None = None) -> SessionSummary:
    return SessionSummary(
        id=id_, title="t", message_count=1, last_at=datetime(2026, 7, 25, tzinfo=UTC), model=model
    )


async def test_sessions_lists_without_a_model_store_wired() -> None:
    memory = _FakeMemory(session_list=[_session("s1")])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/sessions")
    assert resp.status_code == 200
    assert resp.json()[0]["model"] is None


async def test_sessions_enriches_with_the_persisted_model_override() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    session_models = SessionModelStore(engine)
    await session_models.init()
    await session_models.set(tenant="local", session_id="s1", model="grok/grok-4.5-latest")
    memory = _FakeMemory(session_list=[_session("s1"), _session("s2")])
    app = _memory_app(memory, session_models=session_models)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/sessions")
    by_id = {s["id"]: s["model"] for s in resp.json()}
    assert by_id == {"s1": "grok/grok-4.5-latest", "s2": None}


async def test_sessions_enrichment_degrades_on_a_lookup_failure() -> None:
    class _BrokenSessionModels:
        async def lookup(self, *, tenant: str, session_ids: list[str]) -> dict[str, str]:
            raise RuntimeError("db down")

    memory = _FakeMemory(session_list=[_session("s1")])
    app = _memory_app(memory, session_models=_BrokenSessionModels())  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/sessions")
    # The list still comes back — a model-lookup hiccup must not empty the chat list.
    assert resp.status_code == 200
    assert resp.json()[0]["model"] is None


async def test_set_session_model_persists_and_returns_it() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    session_models = SessionModelStore(engine)
    await session_models.init()
    app = _memory_app(_FakeMemory(), session_models=session_models)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/platform/v1/agent/sessions/s1/model", json={"model": "grok/grok-4.5-latest"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"model": "grok/grok-4.5-latest"}
    assert await session_models.get(tenant="local", session_id="s1") == "grok/grok-4.5-latest"


# ── the model a turn actually runs on (#707, ADR-0111) ───────────────────────
#
# The session row is the owner of truth, resolved server-side at turn time. These assert the
# behaviour a client cannot be trusted to get right: it sends its own device default on every
# turn, and a turn sent before its cached sessions list catches up must still run the model the
# conversation was actually given.


CHAT = "/platform/v1/agent/chat"
# The caller's default on every turn, exactly as the web sends it.
BODY: dict[str, Any] = {"messages": [], "model": "llama3.2"}


class _RecordingAgent:
    """Records the model each turn was started with — the only thing these tests assert."""

    def __init__(self) -> None:
        self.models: list[str | None] = []

    async def run(
        self,
        messages: object,
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurn:
        self.models.append(model)
        return AgentTurn(content="hi", stopped="completed")

    async def run_stream(
        self,
        messages: object,
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        persist_input: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        self.models.append(model)
        yield AgentEvent(type="delta", text="hi")
        yield AgentEvent(type="done", turn=AgentTurn(content="hi", stopped="completed"))


async def _model_store(session_id: str | None = None, model: str = "grok/grok-4.5-latest") -> Any:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = SessionModelStore(engine)
    await store.init()
    if session_id is not None:
        await store.set(tenant="local", session_id=session_id, model=model)
    return store


async def _post(app: FastAPI, path: str, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, json=body)


async def test_a_turn_runs_the_sessions_model_over_the_callers_default() -> None:
    # The point of the whole feature: set_chat_model wrote the row, and the next turn honours it
    # even though the client is still sending the device default it had before the switch.
    agent = _RecordingAgent()
    app = _memory_app(_FakeMemory(), session_models=await _model_store("s1"), agent=agent)
    resp = await _post(app, CHAT, {**BODY, "session_id": "s1"})
    assert resp.status_code == 200
    assert agent.models == ["grok/grok-4.5-latest"]


async def test_a_session_with_no_model_falls_back_to_the_callers_default() -> None:
    # A brand-new chat: nothing has been chosen for it, so the caller's own default is right.
    agent = _RecordingAgent()
    app = _memory_app(_FakeMemory(), session_models=await _model_store(), agent=agent)
    resp = await _post(app, CHAT, {**BODY, "session_id": "s1"})
    assert resp.status_code == 200
    assert agent.models == ["llama3.2"]


async def test_clearing_the_override_hands_the_model_back_to_the_caller() -> None:
    # Picking "core default" back in the picker deletes the row; the next turn must not keep
    # running the model that row used to name.
    agent = _RecordingAgent()
    store = await _model_store("s1")
    await store.clear(tenant="local", session_id="s1")
    app = _memory_app(_FakeMemory(), session_models=store, agent=agent)
    resp = await _post(app, CHAT, {**BODY, "session_id": "s1"})
    assert resp.status_code == 200
    assert agent.models == ["llama3.2"]


async def test_a_turn_without_a_session_never_consults_the_store() -> None:
    # A one-off turn (no session_id) has no row to consult; it must not error looking for one.
    agent = _RecordingAgent()
    app = _memory_app(_FakeMemory(), session_models=await _model_store("s1"), agent=agent)
    resp = await _post(app, CHAT, dict(BODY))
    assert resp.status_code == 200
    assert agent.models == ["llama3.2"]


async def test_a_model_lookup_failure_degrades_to_the_callers_default() -> None:
    # A capability *hint* must never cost the user their turn — a store hiccup falls back to
    # exactly the pre-override behaviour rather than 500ing.
    class _BrokenSessionModels:
        async def get(self, *, tenant: str, session_id: str) -> str | None:
            raise RuntimeError("db down")

    agent = _RecordingAgent()
    app = _memory_app(
        _FakeMemory(),
        session_models=_BrokenSessionModels(),  # type: ignore[arg-type]
        agent=agent,
    )
    resp = await _post(app, CHAT, {**BODY, "session_id": "s1"})
    assert resp.status_code == 200
    assert agent.models == ["llama3.2"]


async def test_the_streamed_turn_resolves_the_session_model_too() -> None:
    # /chat/stream is the path the web app actually uses — resolving only in /chat would fix
    # nothing a user can see.
    agent = _RecordingAgent()
    app = _memory_app(_FakeMemory(), session_models=await _model_store("s1"), agent=agent)
    resp = await _post(app, CHAT + "/stream", {**BODY, "session_id": "s1"})
    assert resp.status_code == 200
    assert agent.models == ["grok/grok-4.5-latest"]


async def test_regenerating_keeps_the_sessions_model() -> None:
    # Re-answering must not silently switch the conversation back to the device default.
    agent = _RecordingAgent()
    app = _memory_app(_FakeMemory(), session_models=await _model_store("s1"), agent=agent)
    resp = await _post(app, "/platform/v1/agent/sessions/s1/regenerate", {"model": "llama3.2"})
    assert resp.status_code == 200
    assert agent.models == ["grok/grok-4.5-latest"]


async def test_set_session_model_requires_a_store() -> None:
    app = _memory_app(_FakeMemory())  # no session_models wired
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/platform/v1/agent/sessions/s1/model", json={"model": "x"})
    assert resp.status_code == 503


async def test_set_session_model_rejects_a_blank_model() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    session_models = SessionModelStore(engine)
    await session_models.init()
    app = _memory_app(_FakeMemory(), session_models=session_models)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/platform/v1/agent/sessions/s1/model", json={"model": "   "})
    assert resp.status_code == 400
    assert await session_models.get(tenant="local", session_id="s1") is None


async def test_set_session_model_null_clears_the_override() -> None:
    # Picking "core default" back in the picker (#707) — the tool itself never clears,
    # only this explicit path can.
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    session_models = SessionModelStore(engine)
    await session_models.init()
    await session_models.set(tenant="local", session_id="s1", model="qwen2.5:7b")
    app = _memory_app(_FakeMemory(), session_models=session_models)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/platform/v1/agent/sessions/s1/model", json={"model": None})
    assert resp.status_code == 200
    assert resp.json() == {"model": None}
    assert await session_models.get(tenant="local", session_id="s1") is None


async def test_memory_list_returns_corpus_and_total() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(_FakeMemory())), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == ["f2", "f1"]  # newest first
    assert body["items"][0]["source"] == "auto"
    assert body["items"][0]["score"] is None  # corpus rows carry no score


async def test_memory_search_trims_and_forwards_the_query() -> None:
    memory = _FakeMemory()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/memory", params={"q": "  apples "})
    assert resp.status_code == 200
    assert memory.searched == ["apples"]  # whitespace trimmed before search
    assert resp.json()["items"][0]["score"] == 0.9


async def test_memory_list_rejects_out_of_range_limit() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(_FakeMemory())), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/memory", params={"limit": 0})
    assert resp.status_code == 422  # limit has ge=1


async def test_forget_memory_deletes_one_fact() -> None:
    memory = _FakeMemory()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.delete("/platform/v1/agent/memory/f7")
    assert resp.status_code == 200
    assert resp.json() == {"forgotten": 1}
    assert memory.forgotten == ["f7"]


# ── standing profile (#527, ADR-0094) ────────────────────────────────────────


async def _fresh_profile_store() -> StandingProfileStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = StandingProfileStore(engine)
    await store.init()
    return store


def _profile_app(profile: StandingProfileStore, memory: object | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_agent_router(
            _FakeAgent(),  # type: ignore[arg-type]
            memory or _FakeMemory(),  # type: ignore[arg-type]
            "local",
            object(),  # attachment store — unused by the profile routes  # type: ignore[arg-type]
            profile=profile,
        )
    )
    return app


async def test_profile_get_is_null_when_none_synthesized() -> None:
    store = await _fresh_profile_store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_profile_app(store)), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/memory/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"] is None
    assert body["pinned"] is False


async def test_profile_put_pins_an_operator_edit() -> None:
    store = await _fresh_profile_store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_profile_app(store)), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/platform/v1/agent/memory/profile", json={"content": "My own words about me."}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["content"] == "My own words about me."
    assert body["source"] == "edited"
    assert body["pinned"] is True  # an operator edit is pinned (survives re-synthesis)
    latest = await store.latest(tenant="local")
    assert latest is not None and latest.source == "edited"


async def test_profile_put_empty_content_clears() -> None:
    store = await _fresh_profile_store()
    await store.save(tenant="local", content="auto profile", source="auto")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_profile_app(store)), base_url="http://test"
    ) as client:
        resp = await client.put("/platform/v1/agent/memory/profile", json={"content": "   "})
    assert resp.status_code == 200
    assert resp.json()["profile"] is None
    assert await store.latest(tenant="local") is None  # cleared


async def test_profile_delete_clears_without_hitting_the_forget_fact_route() -> None:
    # DELETE /memory/profile must not be captured as DELETE /memory/{memory_id="profile"} — the
    # route is declared before the fact-forget route precisely to avoid that (FastAPI matches
    # in declaration order).
    store = await _fresh_profile_store()
    await store.save(tenant="local", content="x", source="auto")
    memory = _FakeMemory()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_profile_app(store, memory)), base_url="http://test"
    ) as client:
        resp = await client.delete("/platform/v1/agent/memory/profile")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 1}
    assert memory.forgotten == []  # the fact-forget route was NOT hit
    assert await store.latest(tenant="local") is None


# ── regenerate / edit the conversation tail (#302) ───────────────────────────


async def test_regenerate_truncates_then_streams_a_fresh_turn() -> None:
    memory = _FakeMemory(last_user=5)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/agent/sessions/s1/regenerate", json={})
    assert resp.status_code == 200
    # The stale tail after the last user message (id 5) is dropped, then the turn streams.
    assert memory.truncated_after == [5]
    assert [event for event, _ in _parse_sse(resp.text)] == ["delta", "done"]


async def test_regenerate_with_no_user_turn_errors() -> None:
    memory = _FakeMemory(last_user=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/agent/sessions/s1/regenerate", json={})
    frames = _parse_sse(resp.text)
    assert [event for event, _ in frames] == ["error"]
    assert memory.truncated_after == []  # nothing dropped when there's nothing to answer


async def test_edit_revises_the_last_user_message_then_truncates_and_streams() -> None:
    memory = _FakeMemory(last_user=5)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/agent/sessions/s1/edit", json={"content": "  corrected ask  "}
        )
    assert resp.status_code == 200
    assert memory.revised == [(5, "corrected ask")]  # trimmed, applied to the last user msg
    assert memory.truncated_after == [5]
    assert [event for event, _ in _parse_sse(resp.text)] == ["delta", "done"]


async def test_edit_with_blank_content_errors_and_changes_nothing() -> None:
    memory = _FakeMemory(last_user=5)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/agent/sessions/s1/edit", json={"content": "   "})
    assert [event for event, _ in _parse_sse(resp.text)] == ["error"]
    assert memory.revised == [] and memory.truncated_after == []


# ── edit any user message in history (#552) ──────────────────────────────────


async def _edit(
    memory: object, body: dict[str, object], *, live_runs: LiveRunRegistry | None = None
) -> str:
    """POST /edit with ``body``; returns the response text for SSE parsing."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory, live_runs=live_runs)),
        base_url="http://test",
    ) as client:
        resp = await client.post("/platform/v1/agent/sessions/s1/edit", json=body)
    return resp.text


async def test_edit_targets_the_named_message_and_truncates_exactly_after_it() -> None:
    """Editing message k revises k and drops everything past it — the tail k+1..n goes."""
    memory = _FakeMemory(last_user=9, roles={3: "user"})
    text = await _edit(memory, {"content": "  reworded  ", "message_id": 3})
    assert memory.revised == [(3, "reworded")]  # trimmed, applied to the *named* message
    assert memory.truncated_after == [3]  # not the last user turn (9) — exactly after k
    assert [event for event, _ in _parse_sse(text)] == ["delta", "done"]


async def test_edit_without_message_id_still_targets_the_last_user_message() -> None:
    """#302's callers send no ``message_id``; they must behave exactly as before."""
    memory = _FakeMemory(last_user=5, roles={5: "user"})
    text = await _edit(memory, {"content": "corrected ask"})
    assert memory.revised == [(5, "corrected ask")]
    assert memory.truncated_after == [5]
    assert [event for event, _ in _parse_sse(text)] == ["delta", "done"]


async def test_edit_rejects_an_assistant_message_without_truncating() -> None:
    memory = _FakeMemory(last_user=9, roles={4: "assistant"})
    text = await _edit(memory, {"content": "hi", "message_id": 4})
    assert [event for event, _ in _parse_sse(text)] == ["error"]
    assert memory.revised == [] and memory.truncated_after == []


async def test_edit_rejects_an_id_from_another_session_without_truncating() -> None:
    """An id the session doesn't hold reads as absent — never a truncation anchor."""
    memory = _FakeMemory(last_user=9, roles={3: "user"})  # 77 belongs to some other conversation
    text = await _edit(memory, {"content": "hi", "message_id": 77})
    assert [event for event, _ in _parse_sse(text)] == ["error"]
    assert memory.revised == [] and memory.truncated_after == []


async def test_edit_rejects_a_garbage_id_without_truncating() -> None:
    memory = _FakeMemory(last_user=9, roles={3: "user"})
    text = await _edit(memory, {"content": "hi", "message_id": -1})
    assert [event for event, _ in _parse_sse(text)] == ["error"]
    assert memory.revised == [] and memory.truncated_after == []


async def test_edit_with_a_non_integer_message_id_is_rejected_by_validation() -> None:
    memory = _FakeMemory(last_user=9, roles={3: "user"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_memory_app(memory)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/agent/sessions/s1/edit", json={"content": "hi", "message_id": "three"}
        )
    assert resp.status_code == 422
    assert memory.revised == [] and memory.truncated_after == []


async def test_edit_during_an_active_run_is_rejected_before_anything_is_written() -> None:
    """The guard must precede the write: a live run's history is not ours to cut (#552).

    Without it the route revises + truncates and only *then* hits the one-run 409 in
    ``runs.start`` — leaving the conversation cut short and unanswered.
    """
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    run = await registry.start(
        lambda: _ScriptAgent([AgentEvent(type="delta", text="…")], gate=gate).run_stream(),
        tenant="local",
        session_id="s1",
    )
    memory = _FakeMemory(last_user=9, roles={3: "user"})
    try:
        text = await _edit(memory, {"content": "hi", "message_id": 3}, live_runs=registry)
        assert [event for event, _ in _parse_sse(text)] == ["error"]
        assert memory.revised == [] and memory.truncated_after == []
    finally:
        gate.set()
        await _settle(run)


# ── durable, re-attachable turns (live runs, #376) ───────────────────────────


class _ScriptAgent:
    """Streams a fixed event list, optionally holding open on ``gate`` partway through.

    ``events`` stream first; if ``gate`` is set, it then waits (the run stays *running* for
    re-attach / conflict tests) before streaming ``after_gate``. Accepts any run_stream kwargs
    (model / session_id / persist_input / resume_convo) so it stands in everywhere the router
    drives the agent."""

    def __init__(
        self,
        events: list[AgentEvent],
        *,
        gate: asyncio.Event | None = None,
        after_gate: list[AgentEvent] | None = None,
    ) -> None:
        self._events = list(events)
        self._gate = gate
        self._after_gate = list(after_gate or [])

    async def run_stream(
        self, messages: object = None, **_kwargs: object
    ) -> AsyncIterator[AgentEvent]:
        for event in self._events:
            yield event
        if self._gate is not None:
            await self._gate.wait()
        for event in self._after_gate:
            yield event


def _runs_app(
    agent: object,
    *,
    registry: LiveRunRegistry,
    probe: object | None = None,
    suspended: object | None = None,
    pending_drafts: object | None = None,
    send_draft: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_agent_router(
            agent,  # type: ignore[arg-type]
            object(),  # memory — unused by the live-run routes  # type: ignore[arg-type]
            "local",
            object(),  # attachment store — unused  # type: ignore[arg-type]
            probe=probe,  # type: ignore[arg-type]
            suspended=suspended,  # type: ignore[arg-type]
            pending_drafts=pending_drafts,  # type: ignore[arg-type]
            send_draft=send_draft,  # type: ignore[arg-type]
            live_runs=registry,
        )
    )
    return app


def _parse_sse_ids(text: str) -> list[tuple[str | None, str]]:
    """(id, event) per frame — ``id`` is ``None`` when the frame carried no ``id:`` line."""
    frames: list[tuple[str | None, str]] = []
    for block in text.split("\n\n"):
        event = "message"
        data = ""
        sse_id: str | None = None
        for line in block.splitlines():
            if line.startswith("id:"):
                sse_id = line[len("id:") :].strip()
            elif line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if data:
            frames.append((sse_id, event))
    return frames


async def _settle(run: LiveRun) -> None:
    """Drive a run to terminal (and drain its driver) by consuming one subscriber."""
    async for _seq, _event in run.subscribe(0):
        pass


async def test_chat_stream_frames_carry_id_seq_but_readiness_does_not() -> None:
    registry = LiveRunRegistry()
    app = _runs_app(_FakeAgent(), registry=registry, probe=_FakeProbe())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/agent/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}], "session_id": "s1"},
        )
    frames = _parse_sse_ids(resp.text)
    # Readiness leads with no id (per-connection, never replayed); the turn carries seq ids.
    assert [sse_id for sse_id, ev in frames if ev == "readiness"] == [None, None]
    assert [(sse_id, ev) for sse_id, ev in frames if ev in ("delta", "done")] == [
        ("1", "delta"),
        ("2", "done"),
    ]


async def test_active_run_reports_inflight_then_null() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    held = _ScriptAgent([], gate=gate, after_gate=[AgentEvent(type="done", turn=None)])
    run = await registry.start(
        lambda: held.run_stream(session_id="s1"), tenant="local", session_id="s1"
    )
    app = _runs_app(_FakeAgent(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        live = await client.get("/platform/v1/agent/sessions/s1/active-run")
        assert live.status_code == 200
        assert live.json()["run_id"] == run.run_id
        gate.set()
        await _settle(run)
        gone = await client.get("/platform/v1/agent/sessions/s1/active-run")
    assert gone.json() is None  # terminal → nothing to re-attach to


async def test_active_runs_lists_sessions_with_a_live_turn() -> None:
    """The conversations-list running indicator (#396): one request → which sessions generate."""
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    held = _ScriptAgent([], gate=gate, after_gate=[AgentEvent(type="done", turn=None)])
    r1 = await registry.start(
        lambda: held.run_stream(session_id="s1"), tenant="local", session_id="s1"
    )
    r2 = await registry.start(
        lambda: held.run_stream(session_id="s2"), tenant="local", session_id="s2"
    )
    app = _runs_app(_FakeAgent(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        live = await client.get("/platform/v1/agent/active-runs")
        assert live.status_code == 200
        assert sorted(live.json()["session_ids"]) == ["s1", "s2"]
        gate.set()
        await _settle(r1)
        await _settle(r2)
        done = await client.get("/platform/v1/agent/active-runs")
    assert done.json()["session_ids"] == []  # terminal runs drop out


async def test_reattach_replays_buffer_after_seq() -> None:
    registry = LiveRunRegistry()
    agent = _ScriptAgent(
        [
            AgentEvent(type="delta", text="a"),
            AgentEvent(type="delta", text="b"),
            AgentEvent(type="done", turn=None),
        ]
    )
    run = await registry.start(lambda: agent.run_stream(), tenant="local", session_id="s1")
    await _settle(run)
    app = _runs_app(_FakeAgent(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/agent/runs/{run.run_id}/stream?after_seq=1")
    assert _parse_sse_ids(resp.text) == [("2", "delta"), ("3", "done")]


async def test_reattach_unknown_run_emits_gone() -> None:
    registry = LiveRunRegistry()
    app = _runs_app(_FakeAgent(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/agent/runs/deadbeef/stream")
    assert [event for event, _ in _parse_sse(resp.text)] == ["gone"]


async def test_reattach_omits_the_readiness_prelude() -> None:
    registry = LiveRunRegistry()
    agent = _ScriptAgent([AgentEvent(type="delta", text="a"), AgentEvent(type="done", turn=None)])
    run = await registry.start(lambda: agent.run_stream(), tenant="local", session_id="s1")
    await _settle(run)
    app = _runs_app(_FakeAgent(), registry=registry, probe=_FakeProbe())  # probe wired…
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/platform/v1/agent/runs/{run.run_id}/stream")
    assert [event for event, _ in _parse_sse(resp.text)] == ["delta", "done"]  # …but not replayed


async def test_chat_stream_conflicts_when_a_run_is_active() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    held = _ScriptAgent([], gate=gate, after_gate=[AgentEvent(type="done", turn=None)])
    run = await registry.start(
        lambda: held.run_stream(session_id="s1"), tenant="local", session_id="s1"
    )
    app = _runs_app(_FakeAgent(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/agent/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}], "session_id": "s1"},
        )
    assert resp.status_code == 409
    assert resp.headers["X-Run-Id"] == run.run_id
    gate.set()
    await registry.drain(timeout=0.5)  # clean up the held run's task


async def test_cancel_active_run_endpoint() -> None:
    registry = LiveRunRegistry()
    app = _runs_app(_FakeAgent(), registry=registry)
    gate = asyncio.Event()
    held = _ScriptAgent([], gate=gate, after_gate=[AgentEvent(type="done", turn=None)])
    run = await registry.start(
        lambda: held.run_stream(session_id="s1"), tenant="local", session_id="s1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # No run for an untouched session.
        empty = await client.delete("/platform/v1/agent/sessions/s-empty/active-run")
        assert empty.json() == {"cancelled": False}
        # The session's in-flight run is cancelled (the explicit Stop).
        hit = await client.delete(f"/platform/v1/agent/sessions/{run.session_id}/active-run")
        assert hit.json() == {"cancelled": True}
    await registry.drain(timeout=0.5)
    assert registry.active_for_session(tenant="local", session_id="s1") is None


async def test_resume_starts_a_durable_run_and_consumes_the_suspension() -> None:
    store = SuspendedRunStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    run_id = await store.save(
        tenant="local",
        session_id="s1",
        model="m",
        pending_call_id="c1",
        question="which file?",
        conversation=[{"role": "user", "content": "open it"}],
    )
    registry = LiveRunRegistry()
    agent = _ScriptAgent([AgentEvent(type="delta", text="ok"), AgentEvent(type="done", turn=None)])
    app = _runs_app(agent, registry=registry, suspended=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/platform/v1/agent/runs/{run_id}/resume", json={"answer": "report.md"}
        )
    frames = _parse_sse_ids(resp.text)
    assert [event for _, event in frames] == ["delta", "done"]
    assert [sse_id for sse_id, _ in frames] == ["1", "2"]  # a durable run → id lines
    assert await store.take(tenant="local", run_id=run_id) is None  # the suspension was consumed


async def test_resume_unknown_run_emits_error() -> None:
    store = SuspendedRunStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    app = _runs_app(_FakeAgent(), registry=LiveRunRegistry(), suspended=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/agent/runs/nope/resume", json={"answer": "x"})
    assert [event for event, _ in _parse_sse(resp.text)] == ["error"]


# ── draft-first send Confirm / Decline (ADR-0085, #563) ───────────────────────


class _CapturingAgent:
    """Captures the resume_convo it is handed, then streams a one-frame done turn."""

    def __init__(self) -> None:
        self.resume_convo: list[Any] | None = None

    async def run_stream(
        self, messages: object = None, *, resume_convo: Any = None, **_kw: object
    ) -> AsyncIterator[AgentEvent]:
        self.resume_convo = resume_convo
        yield AgentEvent(type="done", turn=None)


async def _draft_run(store: PendingDraftStore) -> str:
    return await store.save(
        tenant="local",
        session_id="s1",
        model="m",
        pending_call_id="c1",
        tool="mail_send",
        module="mail",
        summary="Email to bob@x.com — Hi",
        draft={"to": "bob@x.com", "subject": "Hi", "body": "Hello"},
        conversation=[{"role": "user", "content": "email bob"}],
    )


async def test_draft_confirm_transmits_the_reviewed_draft_and_resumes() -> None:
    store = PendingDraftStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    run_id = await _draft_run(store)
    sent: dict[str, Any] = {}

    async def send_draft(module: str, draft: dict[str, Any]) -> str:
        sent["module"] = module
        sent["draft"] = draft
        return "gmail-42"

    agent = _CapturingAgent()
    app = _runs_app(agent, registry=LiveRunRegistry(), pending_drafts=store, send_draft=send_draft)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/platform/v1/agent/runs/{run_id}/draft", json={"decision": "send"}
        )
    assert [event for event, _ in _parse_sse(resp.text)] == ["done"]
    # Exactly one send, with the reviewed draft byte-for-byte.
    assert sent == {
        "module": "mail",
        "draft": {"to": "bob@x.com", "subject": "Hi", "body": "Hello"},
    }
    # The turn resumed with the send outcome as the compose call's tool result.
    assert agent.resume_convo is not None
    last = agent.resume_convo[-1]
    assert last.tool_call_id == "c1" and last.name == "mail_send"
    assert "Sent" in (last.content or "") and "gmail-42" in (last.content or "")
    # The suspension was consumed (a double-submit can't send twice).
    assert await store.take(tenant="local", run_id=run_id) is None


async def test_draft_decline_resumes_without_transmitting() -> None:
    store = PendingDraftStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    run_id = await _draft_run(store)
    calls: list[str] = []

    async def send_draft(module: str, draft: dict[str, Any]) -> str:
        calls.append(module)  # must never be reached on a Decline
        return "should-not-happen"

    agent = _CapturingAgent()
    app = _runs_app(agent, registry=LiveRunRegistry(), pending_drafts=store, send_draft=send_draft)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/platform/v1/agent/runs/{run_id}/draft",
            json={"decision": "decline", "reason": "wrong recipient"},
        )
    assert [event for event, _ in _parse_sse(resp.text)] == ["done"]
    assert calls == []  # Decline never transmits
    assert agent.resume_convo is not None
    content = agent.resume_convo[-1].content or ""
    assert "declined" in content and "wrong recipient" in content
    assert await store.take(tenant="local", run_id=run_id) is None  # consumed


async def test_draft_confirm_send_failure_resumes_with_the_hint() -> None:
    store = PendingDraftStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    run_id = await _draft_run(store)

    async def send_draft(module: str, draft: dict[str, Any]) -> str:
        raise HTTPException(status_code=403, detail="Reconnect Google to grant send.")

    agent = _CapturingAgent()
    app = _runs_app(agent, registry=LiveRunRegistry(), pending_drafts=store, send_draft=send_draft)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/platform/v1/agent/runs/{run_id}/draft", json={"decision": "send"}
        )
    # The turn still resumes (the model relays the failure); the draft is consumed.
    assert [event for event, _ in _parse_sse(resp.text)] == ["done"]
    assert agent.resume_convo is not None
    content = agent.resume_convo[-1].content or ""
    assert content.startswith("error:") and "Reconnect Google" in content
    assert await store.take(tenant="local", run_id=run_id) is None


async def test_draft_unknown_run_emits_error() -> None:
    store = PendingDraftStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    app = _runs_app(_FakeAgent(), registry=LiveRunRegistry(), pending_drafts=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/agent/runs/nope/draft", json={"decision": "send"})
    assert [event for event, _ in _parse_sse(resp.text)] == ["error"]
