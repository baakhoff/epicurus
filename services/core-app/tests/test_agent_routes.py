"""Tests for the agent HTTP surface — the readiness-led chat stream (ADR-0027).

The agent loop, readiness probe, memory, and attachment store are all faked; these
tests assert the route's SSE choreography, not model behavior.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from epicurus_core_app.agent import routes as agent_routes
from epicurus_core_app.agent.agent import AgentEvent, AgentTurn
from epicurus_core_app.agent.routes import create_agent_router
from epicurus_core_app.llm.models import PowerState
from epicurus_core_app.memory.memory import MemoryItem
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

    def __init__(self) -> None:
        self.searched: list[str] = []
        self.forgotten: list[str] = []

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


def _memory_app(memory: object) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_agent_router(
            _FakeAgent(),  # type: ignore[arg-type]
            memory,  # type: ignore[arg-type]
            "local",
            object(),  # attachment store — unused by the memory routes  # type: ignore[arg-type]
        )
    )
    return app


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
