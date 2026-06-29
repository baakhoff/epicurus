"""Unit tests for the streaming agent loop — gateway and MCP host are faked."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core_app.agent.agent import (
    _ANSWER_NUDGE,
    _EMPTY_ANSWER_FALLBACK,
    Agent,
    AgentEvent,
)
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.llm.models import ChatMessage, ChatResult, StreamEvent


class _FakeStreamGateway:
    """Replays scripted rounds: each round is (deltas, result)."""

    def __init__(self, rounds: list[tuple[list[str], ChatResult]]) -> None:
        self._rounds = list(rounds)
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: Any = None,
        tenant_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        deltas, result = self._rounds.pop(0)
        for delta in deltas:
            yield StreamEvent(delta=delta)
        yield StreamEvent(result=result)

    async def supports_tools(self, *_a: Any, **_k: Any) -> bool:
        return True


class _FakeMcp:
    def __init__(self, outputs: dict[str, str] | None = None, fail: bool = False) -> None:
        self._outputs = outputs or {}
        self._fail = fail

    async def discover(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs = [{"type": "function", "function": {"name": "echo"}}]
        return specs, {"echo": "http://echo:8080/mcp"}

    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
        if self._fail:
            raise RuntimeError("boom")
        return self._outputs.get(name, "out")


def _tool_call(name: str = "echo", arguments: str = "{}") -> dict[str, Any]:
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}


async def _collect(agent: Agent, text: str) -> list[AgentEvent]:
    return [e async for e in agent.run_stream([ChatMessage(role="user", content=text)])]


async def test_stream_plain_answer() -> None:
    gw = _FakeStreamGateway([(["hel", "lo"], ChatResult(model="m", content="hello"))])
    events = await _collect(Agent(gateway=gw, mcp=_FakeMcp()), "hi")  # type: ignore[arg-type]

    assert [e.type for e in events] == ["delta", "delta", "done"]
    assert events[-1].turn is not None
    assert events[-1].turn.content == "hello"
    assert events[-1].turn.stopped == "completed"


async def test_stream_tool_round_then_answer() -> None:
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call()])),
            (["the echo ", "answered"], ChatResult(model="m", content="the echo answered")),
        ]
    )
    events = await _collect(
        Agent(gateway=gw, mcp=_FakeMcp(outputs={"echo": "pong"})),
        "use echo",  # type: ignore[arg-type]
    )

    assert [e.type for e in events] == ["tool", "tool", "delta", "delta", "done"]
    assert events[0].status == "running" and events[1].status == "ok"
    assert events[-1].turn is not None
    assert events[-1].turn.tools_used == ["echo"]
    # the tool output was fed back into the second round
    assert any(m.role == "tool" and m.content == "pong" for m in gw.calls[1])


async def test_stream_tool_failure_is_reported_not_fatal() -> None:
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call()])),
            (["recovered"], ChatResult(model="m", content="recovered")),
        ]
    )
    events = await _collect(Agent(gateway=gw, mcp=_FakeMcp(fail=True)), "go")  # type: ignore[arg-type]

    tool_events = [e for e in events if e.type == "tool"]
    assert tool_events[-1].status == "error"
    assert events[-1].type == "done"
    assert events[-1].turn is not None and events[-1].turn.content == "recovered"


async def test_stream_gateway_error_yields_error_event() -> None:
    class _Exploding:
        async def supports_tools(self, *args: Any, **kwargs: Any) -> bool:
            return True

        async def stream_chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            raise RuntimeError("paused")
            yield StreamEvent()  # pragma: no cover - makes this an async generator

    events = await _collect(Agent(gateway=_Exploding(), mcp=_FakeMcp()), "hi")  # type: ignore[arg-type]
    assert [e.type for e in events] == ["error"]
    assert events[0].detail == "paused"


async def test_stream_emits_thinking_events_and_persists_them() -> None:
    # A reasoning model streams a `reasoning` event before its answer; the agent surfaces it
    # as a `thinking` event and folds it into the turn's persisted activity (ADR-0041).
    class _ReasoningGateway:
        async def supports_tools(self, *_a: Any, **_k: Any) -> bool:
            return True

        async def stream_chat(
            self,
            messages: list[ChatMessage],
            *,
            model: str | None = None,
            tools: Any = None,
            tenant_id: str | None = None,
        ) -> AsyncIterator[StreamEvent]:
            yield StreamEvent(reasoning="let me ")
            yield StreamEvent(reasoning="think")
            yield StreamEvent(delta="answer")
            yield StreamEvent(
                result=ChatResult(model="m", content="answer", reasoning="let me think")
            )

    agent = Agent(gateway=_ReasoningGateway(), mcp=_FakeMcp())  # type: ignore[arg-type]
    events = await _collect(agent, "hi")

    assert [e.type for e in events] == ["thinking", "thinking", "delta", "done"]
    assert [e.text for e in events if e.type == "thinking"] == ["let me ", "think"]
    turn = events[-1].turn
    assert turn is not None
    assert turn.activity.thinking == "let me think"
    assert turn.activity.steps == []


async def test_stream_tool_steps_are_captured_in_activity() -> None:
    gw = _FakeStreamGateway(
        [
            (
                [],
                ChatResult(model="m", content="", tool_calls=[_tool_call(arguments='{"q": "x"}')]),
            ),
            (["ok"], ChatResult(model="m", content="ok")),
        ]
    )
    events = await _collect(
        Agent(gateway=gw, mcp=_FakeMcp(outputs={"echo": "pong"})),  # type: ignore[arg-type]
        "use echo",
    )

    turn = events[-1].turn
    assert turn is not None
    assert len(turn.activity.steps) == 1
    step = turn.activity.steps[0]
    assert step.tool == "echo" and step.status == "ok"
    assert step.detail == '{"q": "x"}'  # the call's arguments, compact JSON
    # both the running and settled tool events carry the same glanceable detail
    assert [e.detail for e in events if e.type == "tool"] == ['{"q": "x"}', '{"q": "x"}']


async def test_stream_max_steps_forces_final_answer() -> None:
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call()])),
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call()])),
            (["final"], ChatResult(model="m", content="final")),
        ]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(), max_steps=2)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="loop")])]

    assert events[-1].type == "done"
    assert events[-1].turn is not None
    assert events[-1].turn.stopped == "max_steps"
    assert events[-1].turn.content == "final"


async def test_stream_blank_step_is_nudged_into_an_answer() -> None:
    # A reasoning model streams thinking but no answer/tool; the loop nudges it once and it
    # answers on the retry, rather than ending the turn empty (the silent "stop").
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="")),
            (["the ", "answer"], ChatResult(model="m", content="the answer")),
        ]
    )
    events = await _collect(Agent(gateway=gw, mcp=_FakeMcp()), "go")  # type: ignore[arg-type]
    assert events[-1].type == "done"
    assert events[-1].turn is not None and events[-1].turn.content == "the answer"
    assert any(m.role == "user" and m.content == _ANSWER_NUDGE for m in gw.calls[1])


async def test_stream_empty_turn_falls_back_to_a_message() -> None:
    # The model says nothing even after the nudge: the stream emits the fallback as a delta and
    # the persisted turn carries it — never an empty bubble.
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="")),
            ([], ChatResult(model="m", content="")),
        ]
    )
    events = await _collect(Agent(gateway=gw, mcp=_FakeMcp()), "go")  # type: ignore[arg-type]
    assert events[-1].type == "done"
    assert events[-1].turn is not None
    assert events[-1].turn.content == _EMPTY_ANSWER_FALLBACK
    assert any(e.type == "delta" and e.text == _EMPTY_ANSWER_FALLBACK for e in events)


async def test_stream_timeline_preserves_think_tool_think_order() -> None:
    # A reasoning model thinks, calls a tool, then thinks again before answering. The
    # persisted timeline must keep that interleaved order — not "all thinking, then tools".
    class _ScriptGateway:
        def __init__(self, rounds: list[tuple[list[str], list[str], ChatResult]]) -> None:
            self._rounds = list(rounds)

        async def supports_tools(self, *_a: Any, **_k: Any) -> bool:
            return True

        async def stream_chat(
            self,
            messages: list[ChatMessage],
            *,
            model: str | None = None,
            tools: Any = None,
            tenant_id: str | None = None,
        ) -> AsyncIterator[StreamEvent]:
            reasoning, deltas, result = self._rounds.pop(0)
            for r in reasoning:
                yield StreamEvent(reasoning=r)
            for d in deltas:
                yield StreamEvent(delta=d)
            yield StreamEvent(result=result)

    tool_round = ChatResult(model="m", content="", tool_calls=[_tool_call()])
    gw = _ScriptGateway(
        [
            (["plan: ", "search"], [], tool_round),
            (["now answer"], ["done"], ChatResult(model="m", content="done")),
        ]
    )
    events = await _collect(
        Agent(gateway=gw, mcp=_FakeMcp(outputs={"echo": "pong"})),  # type: ignore[arg-type]
        "go",
    )
    turn = events[-1].turn
    assert turn is not None
    items = [i.model_dump() for i in turn.activity.timeline]
    assert [i["kind"] for i in items] == ["thinking", "tool", "thinking"]
    assert items[0]["text"] == "plan: search"  # consecutive reasoning coalesced
    assert items[1]["tool"] == "echo"
    assert items[2]["text"] == "now answer"
    # the flat fields are still derived for back-compat
    assert turn.activity.thinking == "plan: searchnow answer"
    assert [s.tool for s in turn.activity.steps] == ["echo"]


async def test_reanswer_streams_from_stored_tail_without_a_new_user_message() -> None:
    # run_stream([], persist_input=False) re-answers the stored history (regenerate/edit, #302):
    # no new user message is persisted, and the recall query falls back to the last stored turn.
    class _Mem:
        def __init__(self) -> None:
            self.remembered: list[tuple[str, str]] = []

        async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
            assert query == "the original question"  # fell back to the stored user turn
            return ["recalled: the user likes tea"]

        async def history(self, *, tenant: str, session_id: str) -> list[ChatMessage]:
            return [ChatMessage(role="user", content="the original question")]

        async def remember(
            self, *, tenant: str, session_id: str, role: str, content: str, **kw: Any
        ) -> None:
            self.remembered.append((role, content))

    mem = _Mem()
    gw = _FakeStreamGateway([(["fresh ", "answer"], ChatResult(model="m", content="fresh answer"))])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=mem)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([], session_id="s1", persist_input=False)]

    assert events[-1].type == "done"
    assert events[-1].turn is not None and events[-1].turn.content == "fresh answer"
    # Only the assistant answer is persisted — no duplicate user row.
    assert [role for role, _ in mem.remembered] == ["assistant"]
    # The model saw the recalled context + the stored user turn.
    sent = gw.calls[0]
    assert any(m.role == "system" and "tea" in (m.content or "") for m in sent)
    assert any(m.role == "user" and m.content == "the original question" for m in sent)


# ── ask_user pause / resume (ADR-0053) ────────────────────────────────────────


async def _suspend_store() -> SuspendedRunStore:
    store = SuspendedRunStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


def _ask_user_call(question: str, call_id: str = "c1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "ask_user", "arguments": json.dumps({"question": question})},
    }


async def test_ask_user_suspends_the_turn() -> None:
    store = await _suspend_store()
    gw = _FakeStreamGateway(
        [([], ChatResult(model="m", content="", tool_calls=[_ask_user_call("which file?")]))]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(), suspended=store)  # type: ignore[arg-type]
    events = [
        e
        async for e in agent.run_stream(
            [ChatMessage(role="user", content="open the file")], session_id="s1", model="m"
        )
    ]
    types = [e.type for e in events]
    assert "awaiting_input" in types  # the turn paused…
    assert "done" not in types  # …and did not complete
    awaiting = next(e for e in events if e.type == "awaiting_input")
    assert awaiting.question == "which file?"
    assert awaiting.run_id
    # The in-progress run was persisted with the assistant's tool-call message.
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    assert run.pending_call_id == "c1"
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in run.conversation)


async def test_ask_user_resume_continues_the_turn() -> None:
    store = await _suspend_store()
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_ask_user_call("which file?")])),
            (["the ", "report"], ChatResult(model="m", content="the report")),
        ]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(), suspended=store)  # type: ignore[arg-type]
    first = [
        e
        async for e in agent.run_stream(
            [ChatMessage(role="user", content="open it")], session_id="s1", model="m"
        )
    ]
    awaiting = next(e for e in first if e.type == "awaiting_input")
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    convo = [ChatMessage.model_validate(m) for m in run.conversation]
    convo.append(
        ChatMessage(
            role="tool", tool_call_id=run.pending_call_id, name="ask_user", content="report.md"
        )
    )
    resumed = [
        e async for e in agent.run_stream([], session_id="s1", model="m", resume_convo=convo)
    ]
    assert resumed[-1].type == "done"
    assert resumed[-1].turn is not None
    assert resumed[-1].turn.content == "the report"
    # The model continued the same turn with the user's answer as the ask_user tool result.
    assert any(m.role == "tool" and m.content == "report.md" for m in gw.calls[1])


async def test_ask_user_without_store_degrades_and_answers() -> None:
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_ask_user_call("which?")])),
            (["best guess"], ChatResult(model="m", content="best guess")),
        ]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp())  # no suspend store wired  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    assert "awaiting_input" not in [e.type for e in events]
    assert events[-1].type == "done"
    assert events[-1].turn is not None and events[-1].turn.content == "best guess"
    # Without a store the loop feeds an instruction back as the ask_user result and continues.
    assert any(
        m.role == "tool" and m.name == "ask_user" and (m.content or "").startswith("error:")
        for m in gw.calls[1]
    )


async def test_persist_answer_is_shielded_from_cancellation() -> None:
    # The model already produced the answer; a cancellation arriving during the persist (server
    # shutdown — the turn runs in a detached task, #376) must still flush it. That's the
    # asyncio.shield around _persist_answer: cancel mid-write, the answer still lands.
    persisting = asyncio.Event()
    release = asyncio.Event()

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
            if role == "assistant":
                persisting.set()
                await release.wait()  # hold the assistant write open across the cancellation
            self.remembered.append((role, content))

    mem = _Mem()
    gw = _FakeStreamGateway([(["done"], ChatResult(model="m", content="done"))])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=mem)  # type: ignore[arg-type]

    async def consume() -> None:
        async for _ in agent.run_stream([ChatMessage(role="user", content="hi")], session_id="s1"):
            pass

    task = asyncio.create_task(consume())
    await persisting.wait()  # run_stream is now inside the shielded _persist_answer
    task.cancel()  # as if shutting down
    release.set()  # let the shielded write proceed
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)  # let the shielded coroutine finish recording
    assert ("assistant", "done") in mem.remembered  # persisted despite the cancellation


async def test_ask_user_runs_sibling_tools_before_suspending() -> None:
    store = await _suspend_store()
    gw = _FakeStreamGateway(
        [
            (
                [],
                ChatResult(
                    model="m",
                    content="",
                    tool_calls=[_tool_call(), _ask_user_call("which?", call_id="c2")],
                ),
            )
        ]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(outputs={"echo": "pong"}), suspended=store)  # type: ignore[arg-type]
    events = [
        e async for e in agent.run_stream([ChatMessage(role="user", content="go")], session_id="s1")
    ]
    awaiting = next(e for e in events if e.type == "awaiting_input")
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    # The sibling tool ran (its result is in the persisted convo) so the convo stays valid;
    # ask_user has no result yet — that arrives on resume.
    assert any(m.get("role") == "tool" and m.get("content") == "pong" for m in run.conversation)
    assert not any(m.get("tool_call_id") == "c2" for m in run.conversation)
