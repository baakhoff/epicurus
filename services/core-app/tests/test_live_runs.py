"""Unit tests for the live-run registry — turns decoupled from the request (#376).

The crux is :func:`test_subscriber_disconnect_does_not_lose_the_answer`: a turn must run to
completion (and persist its answer) even when the client that started it goes away mid-stream
— the regression that left mobile/PWA chats stuck and answers lost.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from epicurus_core_app.agent.agent import Agent, AgentEvent
from epicurus_core_app.agent.live_runs import LiveRunRegistry, RunAlreadyActiveError
from epicurus_core_app.llm.models import ChatMessage, ChatResult, StreamEvent


async def _one_done() -> AsyncIterator[AgentEvent]:
    yield AgentEvent(type="done", turn=None)


async def _delta_delta_done() -> AsyncIterator[AgentEvent]:
    yield AgentEvent(type="delta", text="a")
    yield AgentEvent(type="delta", text="b")
    yield AgentEvent(type="done", turn=None)


def _held(gate: asyncio.Event) -> Any:
    """A run that buffers nothing and stays running until ``gate`` is set."""

    async def run() -> AsyncIterator[AgentEvent]:
        await gate.wait()
        yield AgentEvent(type="done", turn=None)

    return run


async def _drain_to_terminal(run: Any, after_seq: int = 0) -> list[tuple[int, str]]:
    """Consume a subscriber to the end — also the clean way to await terminal in a test."""
    return [(seq, event.type) async for seq, event in run.subscribe(after_seq)]


async def test_subscribe_replays_then_tails_live() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()

    async def gated() -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="delta", text="a")
        await gate.wait()
        yield AgentEvent(type="done", turn=None)

    run = await registry.start(gated, tenant="local", session_id=None)
    collected: list[tuple[int, str]] = []

    async def consume() -> None:
        async for seq, event in run.subscribe(0):
            collected.append((seq, event.type))

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    assert collected == [(1, "delta")]  # tailing the live buffer before it finished
    gate.set()
    await task
    assert collected == [(1, "delta"), (2, "done")]


async def test_late_subscriber_replays_full_buffer_after_completion() -> None:
    registry = LiveRunRegistry()
    run = await registry.start(_delta_delta_done, tenant="local", session_id=None)
    # Subscribing only after the run finished still yields the whole buffer (no hang).
    assert await _drain_to_terminal(run) == [(1, "delta"), (2, "delta"), (3, "done")]


async def test_two_concurrent_subscribers_see_identical_streams() -> None:
    registry = LiveRunRegistry()
    run = await registry.start(_delta_delta_done, tenant="local", session_id=None)
    first, second = await asyncio.gather(_drain_to_terminal(run), _drain_to_terminal(run))
    assert first == second == [(1, "delta"), (2, "delta"), (3, "done")]


async def test_after_seq_skips_already_seen_events() -> None:
    registry = LiveRunRegistry()
    run = await registry.start(_delta_delta_done, tenant="local", session_id=None)
    assert await _drain_to_terminal(run, after_seq=1) == [(2, "delta"), (3, "done")]


async def test_subscriber_disconnect_does_not_lose_the_answer() -> None:
    """The core regression: drop the subscriber mid-turn; the turn still finishes + persists."""
    release = asyncio.Event()

    class _GatedGateway:
        async def supports_tools(self, *_a: Any, **_k: Any) -> bool:
            return False

        async def stream_chat(
            self,
            messages: list[ChatMessage],
            *,
            model: str | None = None,
            tools: Any = None,
            tenant_id: str | None = None,
        ) -> AsyncIterator[StreamEvent]:
            yield StreamEvent(delta="par")
            await release.wait()  # the turn is mid-flight here
            yield StreamEvent(delta="tial")
            yield StreamEvent(result=ChatResult(model="m", content="partial"))

    class _NoMcp:
        async def discover(
            self, *, allow: frozenset[str] | None = None
        ) -> tuple[list[dict[str, Any]], dict[str, str]]:
            return [], {}

    class _Mem:
        def __init__(self) -> None:
            self.remembered: list[tuple[str, str]] = []

        async def history(self, *, tenant: str, session_id: str) -> list[ChatMessage]:
            return []

        async def recall(self, *, tenant: str, query: str, limit: int = 8) -> list[str]:
            return []

        async def remember(
            self, *, tenant: str, session_id: str, role: str, content: str, **_kw: Any
        ) -> None:
            self.remembered.append((role, content))

    mem = _Mem()
    agent = Agent(gateway=_GatedGateway(), mcp=_NoMcp(), memory=mem)  # type: ignore[arg-type]
    registry = LiveRunRegistry()
    run = await registry.start(
        lambda: agent.run_stream([ChatMessage(role="user", content="hi")], session_id="s1"),
        tenant="local",
        session_id="s1",
    )

    # A client attaches, reads the first token, then disconnects mid-turn.
    sub = run.subscribe(0)
    _, first = await sub.__anext__()
    assert first.type == "delta"
    await sub.aclose()

    # The turn keeps running regardless; let it finish and confirm the answer persisted.
    release.set()
    tail = [event.type async for _, event in run.subscribe(0)]
    assert tail[-1] == "done"
    assert run.status == "done"
    assert ("assistant", "partial") in mem.remembered  # the durable write happened


async def test_one_running_run_per_session_rejects_then_allows() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    run1 = await registry.start(_held(gate), tenant="local", session_id="s1")

    with pytest.raises(RunAlreadyActiveError) as excinfo:
        await registry.start(_held(gate), tenant="local", session_id="s1")
    assert excinfo.value.run_id == run1.run_id

    gate.set()
    await _drain_to_terminal(run1)  # run1 is now terminal
    run2 = await registry.start(_one_done, tenant="local", session_id="s1")
    assert run2.run_id != run1.run_id
    await _drain_to_terminal(run2)


async def test_none_session_skips_the_one_run_guard() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    run1 = await registry.start(_held(gate), tenant="local", session_id=None)
    run2 = await registry.start(_held(gate), tenant="local", session_id=None)
    assert run1.run_id != run2.run_id
    gate.set()
    await asyncio.gather(_drain_to_terminal(run1), _drain_to_terminal(run2))


async def test_active_for_session_is_none_once_terminal() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    run = await registry.start(_held(gate), tenant="local", session_id="s1")
    assert registry.active_for_session(tenant="local", session_id="s1") is run
    gate.set()
    await _drain_to_terminal(run)
    assert registry.active_for_session(tenant="local", session_id="s1") is None


async def test_get_is_tenant_scoped() -> None:
    registry = LiveRunRegistry()
    run = await registry.start(_one_done, tenant="A", session_id="s1")
    await _drain_to_terminal(run)
    assert registry.get(run.run_id, tenant="A") is run
    assert registry.get(run.run_id, tenant="B") is None  # constraint #1


async def test_reap_evicts_finished_runs_after_grace() -> None:
    registry = LiveRunRegistry(grace_seconds=0.0)
    run = await registry.start(_one_done, tenant="local", session_id="s1")
    await _drain_to_terminal(run)
    await asyncio.sleep(0.01)  # let the monotonic clock move past the (zero) grace
    async with registry._lock:  # exercising the reaper directly
        registry._reap_locked()
    assert registry.get(run.run_id, tenant="local") is None
    assert registry.active_for_session(tenant="local", session_id="s1") is None


async def test_driver_synthesizes_error_when_stream_ends_without_terminal() -> None:
    registry = LiveRunRegistry()

    async def no_terminal() -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="delta", text="x")  # ends without done/error/awaiting_input

    run = await registry.start(no_terminal, tenant="local", session_id=None)
    events = [event.type async for _, event in run.subscribe(0)]
    assert events == ["delta", "error"]  # a terminal frame is always synthesized
    assert run.status == "error"


async def test_drain_cancels_inflight_and_marks_terminal() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()  # never set — the run is wedged
    run = await registry.start(_held(gate), tenant="local", session_id="s1")
    await registry.drain(timeout=0.05)
    assert run.terminal
    # A subscriber still unblocks (it sees the synthesized shutdown error), never hangs.
    assert [event.type async for _, event in run.subscribe(0)] == ["error"]


async def test_cancel_stops_a_running_run() -> None:
    registry = LiveRunRegistry()
    gate = asyncio.Event()  # never set — the run keeps running until cancelled
    run = await registry.start(_held(gate), tenant="local", session_id="s1")
    assert not run.terminal
    await registry.cancel(run)
    # The driver unwinds and marks terminal; a subscriber still drains rather than hanging.
    assert [event.type async for _, event in run.subscribe(0)] == ["error"]
    assert run.status == "error"
    assert registry.active_for_session(tenant="local", session_id="s1") is None


async def test_active_sessions_lists_live_sessioned_runs_for_tenant() -> None:
    """The conversations-list running indicator (#396): which sessions are generating now."""
    registry = LiveRunRegistry()
    gate = asyncio.Event()
    r1 = await registry.start(_held(gate), tenant="local", session_id="s1")
    r2 = await registry.start(_held(gate), tenant="local", session_id="s2")
    rother = await registry.start(_held(gate), tenant="other", session_id="s3")
    ranon = await registry.start(_held(gate), tenant="local", session_id=None)

    # This tenant's sessioned, non-terminal runs only — not the foreign tenant (#1), not anon.
    assert sorted(registry.active_sessions(tenant="local")) == ["s1", "s2"]
    assert registry.active_sessions(tenant="other") == ["s3"]

    gate.set()
    await asyncio.gather(*(_drain_to_terminal(r) for r in (r1, r2, rother, ranon)))
    assert registry.active_sessions(tenant="local") == []  # terminal runs drop out
