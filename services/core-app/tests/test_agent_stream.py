"""Unit tests for the streaming agent loop — gateway and MCP host are faked."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import Attachment, EntityRef, WritesDocument, draft_review
from epicurus_core_app.agent.agent import (
    _ANSWER_NUDGE,
    _EMPTY_ANSWER_FALLBACK,
    _REPEAT_NUDGE,
    _STOPPED_REPEAT_CALL,
    _STOPPED_TOOL_ERRORS,
    _STOPPED_UNSUPPORTED_MEDIA,
    _STREAM_STALLED_MESSAGE,
    _TOOL_DETAIL_CAP,
    _VISION_UNSUPPORTED_MESSAGE,
    Agent,
    AgentEvent,
)
from epicurus_core_app.agent.attachments import ExpandedAttachments, ImagePart
from epicurus_core_app.agent.doc_preview import DocumentToolLookup
from epicurus_core_app.agent.mcp_host import ToolCallError
from epicurus_core_app.agent.pending_approvals import PendingApprovalStore
from epicurus_core_app.agent.pending_drafts import PendingDraftStore
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.llm.models import ChatMessage, ChatResult, StreamEvent, ToolCallFragment


class _FakeStreamGateway:
    """Replays scripted rounds: each round is (deltas, result)."""

    def __init__(
        self, rounds: list[tuple[list[str], ChatResult]], *, supports_vision: bool = True
    ) -> None:
        self._rounds = list(rounds)
        self.calls: list[list[ChatMessage]] = []
        self._supports_vision = supports_vision

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

    async def supports_vision(self, *_a: Any, **_k: Any) -> bool:
        return self._supports_vision


class _FakeMcp:
    def __init__(self, outputs: dict[str, str] | None = None, fail: bool = False) -> None:
        self._outputs = outputs or {}
        self._fail = fail

    async def discover(
        self, *, allow: frozenset[str] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs = [{"type": "function", "function": {"name": "echo"}}]
        return specs, {"echo": "http://echo:8080/mcp"}

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        url: str,
        *,
        tenant: str,
        session_id: str | None = None,
    ) -> str:
        if self._fail:
            raise RuntimeError("boom")
        return self._outputs.get(name, "out")


def _text(content: str | list[dict[str, Any]] | None) -> str:
    """Flatten a message's ``content`` to text for assertions.

    ``ChatMessage.content`` is ``str | list[dict] | None``; the parts array only ever rides on
    an assembled vision turn, never on the tool/assistant turns asserted here, so a non-string
    is treated as "no text" exactly like the ``None`` the callers already tolerate.
    """
    return content if isinstance(content, str) else ""


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
        Agent(gateway=gw, mcp=_FakeMcp(outputs={"echo": "pong"})),  # type: ignore[arg-type]
        "use echo",
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


async def test_stream_tool_reported_failure_shows_error_status() -> None:
    # A tool that ran but reported failure (MCP isError → ToolCallError, #435/#440) must stream
    # an `error` status and persist an `error` step — not the green "ok" a text-prefix check
    # gave it, since the tool's own message (fed to the model verbatim) need not begin with
    # "error:". This is the SSE status the web timeline renders (red X vs. green check).
    class _ErrorMcp(_FakeMcp):
        async def call(
            self,
            name: str,
            arguments: dict[str, Any],
            url: str,
            *,
            tenant: str,
            session_id: str | None = None,
        ) -> str:
            raise ToolCallError("Error executing tool echo: event 'e1' not found")

    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call()])),
            (["no such event"], ChatResult(model="m", content="no such event")),
        ]
    )
    events = await _collect(Agent(gateway=gw, mcp=_ErrorMcp()), "go")  # type: ignore[arg-type]

    tool_events = [e for e in events if e.type == "tool"]
    assert [e.status for e in tool_events] == ["running", "error"]
    turn = events[-1].turn
    assert turn is not None
    assert [s.status for s in turn.activity.steps] == ["error"]
    # the model still received the tool's raw message, with no "error:" prefix added
    assert any(
        m.role == "tool" and m.content == "Error executing tool echo: event 'e1' not found"
        for m in gw.calls[1]
    )


async def test_stream_gateway_error_yields_error_event() -> None:
    class _Exploding:
        async def supports_tools(self, *args: Any, **kwargs: Any) -> bool:
            return True

        async def stream_chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            raise RuntimeError("paused")
            yield StreamEvent()  # pragma: no cover - makes this an async generator

    events = await _collect(Agent(gateway=_Exploding(), mcp=_FakeMcp()), "hi")  # type: ignore[arg-type]
    assert [e.type for e in events] == ["error"]
    # A non-connection error with no partial output passes its own short text through — the web
    # keys on "paused" for its paused-state UI, so it must not be rewritten (#453).
    assert events[0].detail == "paused"


class _StallingGateway:
    """Streams some deltas, then raises mid-stream — e.g. the local model going silent and the
    socket read aborting (#453)."""

    def __init__(self, deltas: list[str], exc: Exception) -> None:
        self._deltas = deltas
        self._exc = exc

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
        for delta in self._deltas:
            yield StreamEvent(delta=delta)
        raise self._exc


class _RecordingMem:
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


async def test_stream_socket_timeout_keeps_partial_and_finishes_friendly() -> None:
    # A streaming turn that dies part-way (the litellm/aiohttp socket-read timeout) keeps the
    # partial answer + a friendly note, persists it, and finishes cleanly (#453) — never dumping
    # the raw exception chain into chat or throwing the partial away.
    mem = _RecordingMem()
    exc = RuntimeError(
        "litellm.APIConnectionError: Ollama_chatException - Timeout on reading data from socket"
    )
    gw = _StallingGateway(["Here is ", "the plan"], exc)
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=mem)  # type: ignore[arg-type]
    events = [
        e async for e in agent.run_stream([ChatMessage(role="user", content="hi")], session_id="s1")
    ]

    assert events[-1].type == "done"  # a clean finish, not a raw error bubble
    assert not any(e.type == "error" for e in events)
    # the friendly note streamed; the raw litellm text never did
    assert any(e.type == "delta" and _STREAM_STALLED_MESSAGE in (e.text or "") for e in events)
    assert not any("APIConnectionError" in (e.text or "") for e in events)
    turn = events[-1].turn
    assert turn is not None
    assert turn.content.startswith("Here is the plan")
    assert _STREAM_STALLED_MESSAGE in turn.content
    # persisted, so a reopen still shows the partial (not discarded)
    assert ("assistant", turn.content) in mem.remembered


async def test_stream_failure_before_any_output_yields_friendly_error() -> None:
    # The same failure class with nothing produced yet has no partial to keep: it surfaces a
    # friendly error banner (not the raw litellm text) and stops — no empty persisted turn.
    exc = RuntimeError("APIConnectionError - Timeout on reading data from socket")
    gw = _StallingGateway([], exc)
    events = await _collect(Agent(gateway=gw, mcp=_FakeMcp()), "hi")  # type: ignore[arg-type]

    assert [e.type for e in events] == ["error"]
    assert events[0].detail == _STREAM_STALLED_MESSAGE
    assert "APIConnectionError" not in (events[0].detail or "")


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
    # Distinct args each round, so it's a genuine budget exhaustion (real tool work every step),
    # not the repeated-call path #524's guard intercepts.
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"a": 1}')])),
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"a": 2}')])),
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
    assert awaiting.run_id is not None
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
    agent = Agent(gateway=gw, mcp=_FakeMcp())  # type: ignore[arg-type]  # no suspend store wired
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    assert "awaiting_input" not in [e.type for e in events]
    assert events[-1].type == "done"
    assert events[-1].turn is not None and events[-1].turn.content == "best guess"
    # Without a store the loop feeds an instruction back as the ask_user result and continues.
    assert any(
        m.role == "tool" and m.name == "ask_user" and _text(m.content).startswith("error:")
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
    assert awaiting.run_id is not None
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    # The sibling tool ran (its result is in the persisted convo) so the convo stays valid;
    # ask_user has no result yet — that arrives on resume.
    assert any(m.get("role") == "tool" and m.get("content") == "pong" for m in run.conversation)
    assert not any(m.get("tool_call_id") == "c2" for m in run.conversation)


# ── draft-first send pause / resume (ADR-0085, #563) ──────────────────────────


async def _draft_store() -> PendingDraftStore:
    store = PendingDraftStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


def _draft_envelope(to: str = "bob@x.com", subject: str = "Hi", body: str = "Hello") -> str:
    return draft_review(
        kind="mail",
        module="mail",
        summary=f"Email to {to} — {subject}",
        draft={"to": to, "subject": subject, "body": body},
    )


def _mail_send_call(call_id: str = "c1") -> dict[str, Any]:
    args = json.dumps({"to": "bob@x.com", "subject": "Hi", "body": "Hello"})
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "mail_send", "arguments": args},
    }


class _DraftMcp:
    """An MCP host whose ``mail_send`` tool returns a scripted result (a DraftReview or a hint)."""

    def __init__(self, output: str) -> None:
        self._output = output
        self.calls: list[str] = []

    async def discover(
        self, *, allow: frozenset[str] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs = [{"type": "function", "function": {"name": "mail_send"}}]
        return specs, {"mail_send": "http://mail:8080/mcp"}

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        url: str,
        *,
        tenant: str,
        session_id: str | None = None,
    ) -> str:
        self.calls.append(name)
        return self._output


async def test_draft_review_suspends_the_turn_without_transmitting() -> None:
    store = await _draft_store()
    gw = _FakeStreamGateway(
        [([], ChatResult(model="m", content="", tool_calls=[_mail_send_call()]))]
    )
    mcp = _DraftMcp(_draft_envelope())
    agent = Agent(gateway=gw, mcp=mcp, pending_drafts=store)  # type: ignore[arg-type]
    events = [
        e
        async for e in agent.run_stream(
            [ChatMessage(role="user", content="email bob")], session_id="s1", model="m"
        )
    ]
    types = [e.type for e in events]
    assert "awaiting_input" in types  # the turn paused for review…
    assert "done" not in types  # …and did not complete
    awaiting = next(e for e in events if e.type == "awaiting_input")
    assert awaiting.awaiting_kind == "draft_review"
    assert awaiting.draft == {"to": "bob@x.com", "subject": "Hi", "body": "Hello"}
    assert awaiting.run_id
    # The compose tool ran but nothing was transmitted — the draft is persisted, and the only
    # send path (the module's /send, exercised at the route layer) was never reached here.
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    assert run.pending_call_id == "c1"
    assert run.tool == "mail_send"
    assert run.module == "mail"
    assert run.draft == {"to": "bob@x.com", "subject": "Hi", "body": "Hello"}
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in run.conversation)
    # The compose call has no tool result yet — it is filled on Confirm/Decline.
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in run.conversation
    )


async def test_draft_resume_continues_the_turn() -> None:
    store = await _draft_store()
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_mail_send_call()])),
            (["done, ", "sent"], ChatResult(model="m", content="done, sent")),
        ]
    )
    agent = Agent(gateway=gw, mcp=_DraftMcp(_draft_envelope()), pending_drafts=store)  # type: ignore[arg-type]
    first = [
        e
        async for e in agent.run_stream(
            [ChatMessage(role="user", content="email bob")], session_id="s1", model="m"
        )
    ]
    awaiting = next(e for e in first if e.type == "awaiting_input")
    assert awaiting.run_id is not None
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    convo = [ChatMessage.model_validate(m) for m in run.conversation]
    # The route appends the send outcome under the compose call id (here: a confirmed send).
    convo.append(
        ChatMessage(
            role="tool",
            tool_call_id=run.pending_call_id,
            name=run.tool,
            content="Sent. Provider message id: gmail-42.",
        )
    )
    resumed = [
        e async for e in agent.run_stream([], session_id="s1", model="m", resume_convo=convo)
    ]
    assert resumed[-1].type == "done"
    assert resumed[-1].turn is not None
    assert resumed[-1].turn.content == "done, sent"
    # The model continued the same turn with the send outcome as the mail_send tool result.
    assert any(m.role == "tool" and "Sent" in (m.content or "") for m in gw.calls[1])


async def test_draft_without_store_degrades_and_answers() -> None:
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_mail_send_call()])),
            (["cannot send"], ChatResult(model="m", content="cannot send")),
        ]
    )
    # No pending-draft store wired → the loop degrades instead of pausing.
    agent = Agent(gateway=gw, mcp=_DraftMcp(_draft_envelope()))  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="email bob")])]
    assert "awaiting_input" not in [e.type for e in events]
    assert events[-1].type == "done"
    # Without a store the loop tells the model it could not present a draft, and continues.
    assert any(
        m.role == "tool" and m.name == "mail_send" and _text(m.content).startswith("error:")
        for m in gw.calls[1]
    )


async def test_compose_error_string_does_not_suspend() -> None:
    # A compose that fails (e.g. a missing scope) returns a plain hint, not a DraftReview — it must
    # be fed back to the model as a normal tool result, never paused for review.
    store = await _draft_store()
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_mail_send_call()])),
            (["ok"], ChatResult(model="m", content="ok")),
        ]
    )
    mcp = _DraftMcp("Couldn't reply: reconnect Google to grant the modify permission.")
    agent = Agent(gateway=gw, mcp=mcp, pending_drafts=store)  # type: ignore[arg-type]
    events = [
        e
        async for e in agent.run_stream(
            [ChatMessage(role="user", content="email bob")], session_id="s1", model="m"
        )
    ]
    assert "awaiting_input" not in [e.type for e in events]
    assert events[-1].type == "done"
    assert any(m.role == "tool" and "reconnect Google" in (m.content or "") for m in gw.calls[1])


# ── ask_approval pause / resume (#745, ADR-0117) ──────────────────────────────


async def _approval_store() -> PendingApprovalStore:
    store = PendingApprovalStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


_APPROVAL_REF = {
    "ref_id": "sugg-1",
    "module": "knowledge",
    "kind": "suggestion",
    "title": "Update goals.md",
}


def _ask_approval_call(
    summary: str, refs: list[dict[str, Any]] | None = None, call_id: str = "c1"
) -> dict[str, Any]:
    args = {"summary": summary, "refs": [] if refs is None else refs}
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "ask_approval", "arguments": json.dumps(args)},
    }


async def test_ask_approval_suspends_the_turn() -> None:
    store = await _approval_store()
    gw = _FakeStreamGateway(
        [
            (
                [],
                ChatResult(
                    model="m",
                    content="",
                    tool_calls=[_ask_approval_call("Update goals.md", [_APPROVAL_REF])],
                ),
            )
        ]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(), pending_approvals=store)  # type: ignore[arg-type]
    events = [
        e
        async for e in agent.run_stream(
            [ChatMessage(role="user", content="update my goals note")], session_id="s1", model="m"
        )
    ]
    types = [e.type for e in events]
    assert "awaiting_input" in types  # the turn paused…
    assert "done" not in types  # …and did not complete
    awaiting = next(e for e in events if e.type == "awaiting_input")
    assert awaiting.awaiting_kind == "approval"
    assert awaiting.summary == "Update goals.md"
    assert awaiting.refs == [EntityRef.model_validate(_APPROVAL_REF)]
    assert awaiting.run_id
    # The in-progress run was persisted with the assistant's tool-call message.
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    assert run.pending_call_id == "c1"
    assert run.summary == "Update goals.md"
    assert run.refs == [EntityRef.model_validate(_APPROVAL_REF).model_dump()]
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in run.conversation)
    # The ask_approval call has no tool result yet — it is filled on Approve/Reject.
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in run.conversation
    )


async def test_ask_approval_resume_continues_the_turn() -> None:
    store = await _approval_store()
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_ask_approval_call("Update it")])),
            (["done"], ChatResult(model="m", content="done")),
        ]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(), pending_approvals=store)  # type: ignore[arg-type]
    first = [
        e
        async for e in agent.run_stream(
            [ChatMessage(role="user", content="update it")], session_id="s1", model="m"
        )
    ]
    awaiting = next(e for e in first if e.type == "awaiting_input")
    assert awaiting.run_id is not None
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    convo = [ChatMessage.model_validate(m) for m in run.conversation]
    # The route appends the operator's decision under the ask_approval call id.
    convo.append(
        ChatMessage(
            role="tool",
            tool_call_id=run.pending_call_id,
            name="ask_approval",
            content="The operator approved this change.",
        )
    )
    resumed = [
        e async for e in agent.run_stream([], session_id="s1", model="m", resume_convo=convo)
    ]
    assert resumed[-1].type == "done"
    assert resumed[-1].turn is not None
    assert resumed[-1].turn.content == "done"
    # The model continued the same turn with the decision as the ask_approval tool result.
    assert any(m.role == "tool" and "approved" in (m.content or "") for m in gw.calls[1])


async def test_ask_approval_without_store_degrades_and_answers() -> None:
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_ask_approval_call("Update it")])),
            (["proceeding"], ChatResult(model="m", content="proceeding")),
        ]
    )
    agent = Agent(
        gateway=gw,  # type: ignore[arg-type]
        mcp=_FakeMcp(),  # type: ignore[arg-type]
    )  # no pending-approval store wired
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    assert "awaiting_input" not in [e.type for e in events]
    assert events[-1].type == "done"
    assert events[-1].turn is not None and events[-1].turn.content == "proceeding"
    # Without a store the loop feeds an instruction back as the ask_approval result and continues.
    assert any(
        m.role == "tool" and m.name == "ask_approval" and _text(m.content).startswith("error:")
        for m in gw.calls[1]
    )


async def test_ask_approval_runs_sibling_tools_before_suspending() -> None:
    store = await _approval_store()
    gw = _FakeStreamGateway(
        [
            (
                [],
                ChatResult(
                    model="m",
                    content="",
                    tool_calls=[_tool_call(), _ask_approval_call("Update it", call_id="c2")],
                ),
            )
        ]
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(outputs={"echo": "pong"}), pending_approvals=store)  # type: ignore[arg-type]
    events = [
        e async for e in agent.run_stream([ChatMessage(role="user", content="go")], session_id="s1")
    ]
    awaiting = next(e for e in events if e.type == "awaiting_input")
    assert awaiting.run_id is not None
    run = await store.take(tenant="local", run_id=awaiting.run_id)
    assert run is not None
    # The sibling tool ran (its result is in the persisted convo) so the convo stays valid;
    # ask_approval has no result yet — that arrives on resume.
    assert any(m.get("role") == "tool" and m.get("content") == "pong" for m in run.conversation)
    assert not any(m.get("tool_call_id") == "c2" for m in run.conversation)


async def test_ask_approval_drops_malformed_refs_but_keeps_valid_ones() -> None:
    # The model authors `refs` by hand — a malformed entry (missing a required field, or not
    # even an object) must not fail the whole call, and must not poison the valid entries.
    store = await _approval_store()
    call = _ask_approval_call(
        "Update it",
        refs=[
            _APPROVAL_REF,
            {"ref_id": "missing-fields"},  # invalid: no module/kind/title
            "not even an object",  # type: ignore[list-item]
        ],
    )
    gw = _FakeStreamGateway([([], ChatResult(model="m", content="", tool_calls=[call]))])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), pending_approvals=store)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    awaiting = next(e for e in events if e.type == "awaiting_input")
    assert awaiting.refs == [EntityRef.model_validate(_APPROVAL_REF)]


async def test_ask_approval_missing_refs_argument_defaults_to_empty_list() -> None:
    store = await _approval_store()
    args = json.dumps({"summary": "Update it"})  # `refs` omitted entirely
    call = {"id": "c1", "type": "function", "function": {"name": "ask_approval", "arguments": args}}
    gw = _FakeStreamGateway([([], ChatResult(model="m", content="", tool_calls=[call]))])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), pending_approvals=store)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    awaiting = next(e for e in events if e.type == "awaiting_input")
    assert awaiting.refs == []


# ── Loop hygiene: same rules as run(), applied to run_stream (#524) ───────────


class _CountingMcp(_FakeMcp):
    """A faked MCP that records each tool invocation (and optionally fails every call)."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.calls_made: list[dict[str, Any]] = []

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        url: str,
        *,
        tenant: str,
        session_id: str | None = None,
    ) -> str:
        self.calls_made.append(arguments)
        if self._fail:
            raise ToolCallError("boom: cannot do that")
        return "out"


@pytest.mark.timeout(10)
async def test_stream_repeated_identical_call_nudges_then_stops() -> None:
    # The streamed loop applies the same repeat rule as run(): first repeat nudges, the second
    # stops (repeat_call), the tool runs once, and a real final answer streams — no silent stop.
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")])),
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")])),
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")])),
            (["the ", "answer"], ChatResult(model="m", content="the answer")),
        ]
    )
    mcp = _CountingMcp()
    agent = Agent(gateway=gw, mcp=mcp, max_steps=6)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    done = events[-1]
    assert done.type == "done" and done.turn is not None
    assert done.turn.stopped == _STOPPED_REPEAT_CALL
    assert done.turn.content == "the answer"
    assert len(mcp.calls_made) == 1  # invoked once, not three times
    # the streamed final answer still reaches the user as deltas (never a silent stop)
    assert [e.text for e in events if e.type == "delta"] == ["the ", "answer"]
    # the one-shot repeat nudge was injected after the first repeat (round 3 sees it)
    assert any(m.role == "user" and m.content == _REPEAT_NUDGE for m in gw.calls[2])


@pytest.mark.timeout(10)
async def test_stream_error_streak_stops_early() -> None:
    # Three consecutive tool errors (distinct args → the error-streak path, not repeat) stop the
    # streamed turn early with what failed, rather than exhausting max_steps.
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"try": 1}')])),
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"try": 2}')])),
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"try": 3}')])),
            (["failed"], ChatResult(model="m", content="failed")),
        ]
    )
    mcp = _CountingMcp(fail=True)
    agent = Agent(gateway=gw, mcp=mcp, max_steps=10)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    done = events[-1]
    assert done.turn is not None and done.turn.stopped == _STOPPED_TOOL_ERRORS
    assert done.turn.content == "failed"
    assert len(mcp.calls_made) == 3  # stopped after the 3rd error, well before max_steps=10


@pytest.mark.timeout(10)
async def test_stream_distinct_args_repeats_pass_untouched() -> None:
    # Same tool, different args (paging) must stream through untouched — no nudge, no early stop.
    gw = _FakeStreamGateway(
        [
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"page": 1}')])),
            ([], ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"page": 2}')])),
            (["all ", "read"], ChatResult(model="m", content="all read")),
        ]
    )
    mcp = _CountingMcp()
    agent = Agent(gateway=gw, mcp=mcp, max_steps=6)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([ChatMessage(role="user", content="go")])]
    done = events[-1]
    assert done.turn is not None and done.turn.stopped == "completed"
    assert done.turn.content == "all read"
    assert len(mcp.calls_made) == 2  # both distinct calls ran


# ── image attachments, gated on model vision support (#633) ─────────────────────────


class _FakeExpander:
    def __init__(self, images: list[ImagePart]) -> None:
        self._images = images

    async def expand(self, attachments: list[Attachment], *, tenant: str) -> ExpandedAttachments:
        return ExpandedAttachments(images=self._images)


def _image_part() -> ImagePart:
    return ImagePart(mime="image/png", data_b64="aGVsbG8=", title="photo.png")


def _image_message() -> ChatMessage:
    return ChatMessage(
        role="user",
        content="what is this?",
        attachments=[Attachment(att_id="a1", source="file", title="photo.png")],
    )


async def test_stream_attaches_image_content_when_model_supports_vision() -> None:
    gw = _FakeStreamGateway([(["I see ", "a cat"], ChatResult(model="m", content="I see a cat"))])
    expander = _FakeExpander([_image_part()])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), attachments=expander)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([_image_message()])]

    assert [e.type for e in events] == ["delta", "delta", "done"]
    done = events[-1]
    assert done.turn is not None
    assert done.turn.content == "I see a cat"
    assert done.turn.stopped == "completed"
    [sent] = [m for m in gw.calls[0] if m.role == "user"]
    assert sent.content == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]


async def test_stream_blocks_image_before_any_provider_call_when_model_lacks_vision() -> None:
    gw = _FakeStreamGateway(
        [(["should never stream"], ChatResult(model="m", content="should never stream"))],
        supports_vision=False,
    )
    expander = _FakeExpander([_image_part()])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), attachments=expander)  # type: ignore[arg-type]
    events = [e async for e in agent.run_stream([_image_message()])]

    assert [e.type for e in events] == ["delta", "done"]
    assert events[0].text == _VISION_UNSUPPORTED_MESSAGE
    done = events[-1]
    assert done.turn is not None
    assert done.turn.content == _VISION_UNSUPPORTED_MESSAGE
    assert done.turn.stopped == _STOPPED_UNSUPPORTED_MEDIA
    assert gw.calls == []  # no provider call at all
    assert not any(m.role == "user" and m.content == _REPEAT_NUDGE for c in gw.calls for m in c)


# ── the document pane's tool payload (#541, ADR-0100/0101) ───────────────────


def _writes(content_arg: str = "content", target_arg: str | None = "path") -> WritesDocument:
    return WritesDocument(content_arg=content_arg, target_arg=target_arg)


def _doc_lookup(
    annotation: WritesDocument | None = None, *, module: str = "knowledge", tool: str = "write_doc"
) -> DocumentToolLookup:
    """A registry stand-in: resolves exactly one tool, like the real manifest lookup."""

    async def lookup(name: str) -> tuple[str, WritesDocument] | None:
        if name != tool or annotation is None:
            return None
        return module, annotation

    return lookup


class _ScribeMcp(_FakeMcp):
    """Routes `write_doc` — the base fake only knows `echo`, so a call would error out."""

    async def discover(
        self, *, allow: frozenset[str] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs = [{"type": "function", "function": {"name": "write_doc"}}]
        return specs, {"write_doc": "http://knowledge:8080/mcp"}


def _write_round(arguments: dict[str, Any]) -> list[tuple[list[str], ChatResult]]:
    """One round that calls `write_doc`, then an answer."""
    call = _tool_call("write_doc", json.dumps(arguments))
    return [
        ([], ChatResult(model="m", content="", tool_calls=[call])),
        (["saved"], ChatResult(model="m", content="saved")),
    ]


_DOC_ARGS = {"path": "notes/goals.md", "content": "# Goals\nship it"}


async def _write_events(
    lookup: DocumentToolLookup, arguments: dict[str, Any] | None = None
) -> list[AgentEvent]:
    gw = _FakeStreamGateway(_write_round(_DOC_ARGS if arguments is None else arguments))
    agent = Agent(gateway=gw, mcp=_ScribeMcp(), documents=lookup)  # type: ignore[arg-type]
    return await _collect(agent, "write it down")


async def test_an_annotated_call_says_what_it_is_writing() -> None:
    events = await _write_events(_doc_lookup(_writes()))

    tools = [e for e in events if e.type == "tool"]
    assert [t.status for t in tools] == ["running", "ok"]
    # On both frames: the pane opens on `running` and unlocks on the terminal one.
    for frame in tools:
        assert frame.document == {
            "module": "knowledge",
            "content": "# Goals\nship it",
            "target": "notes/goals.md",
            "title": None,
        }


async def test_an_unannotated_call_carries_no_document() -> None:
    # Every other tool call must be untouched — this is opt-in per tool.
    events = await _write_events(_doc_lookup(None))
    assert all(e.document is None for e in events if e.type == "tool")


async def test_no_lookup_configured_means_no_document() -> None:
    gw = _FakeStreamGateway(_write_round({"path": "a.md", "content": "hi"}))
    events = await _collect(Agent(gateway=gw, mcp=_ScribeMcp()), "go")  # type: ignore[arg-type]
    assert all(e.document is None for e in events if e.type == "tool")


async def test_a_call_with_no_body_opens_no_pane() -> None:
    # The manifest guarantees the argument exists, not that the model filled it. A pane with
    # nothing in it is worse than no pane.
    events = await _write_events(_doc_lookup(_writes()), {"path": "a.md", "content": ""})
    assert all(e.document is None for e in events if e.type == "tool")


async def test_a_non_string_body_opens_no_pane() -> None:
    events = await _write_events(_doc_lookup(_writes()), {"path": "a.md", "content": {"oops": 1}})
    assert all(e.document is None for e in events if e.type == "tool")


async def test_a_missing_target_is_reported_as_absent_not_invented() -> None:
    events = await _write_events(_doc_lookup(_writes()), {"content": "body only"})
    running = next(e for e in events if e.type == "tool")
    assert running.document is not None
    assert running.document["target"] is None
    assert running.document["content"] == "body only"


async def test_a_failing_lookup_never_fails_the_turn() -> None:
    # The pane is an affordance beside the answer: a registry hiccup costs a pane, not the turn.
    async def exploding(_name: str) -> tuple[str, WritesDocument] | None:
        raise RuntimeError("registry down")

    events = await _write_events(exploding)
    assert [e.type for e in events] == ["tool", "tool", "delta", "done"]
    assert events[-1].turn is not None and events[-1].turn.content == "saved"
    assert all(e.document is None for e in events if e.type == "tool")


async def test_the_pane_payload_adds_nothing_to_the_persisted_activity() -> None:
    """The pane rides SSE only — ADR-0041's caps are unchanged by it.

    A document body is unbounded, so it must not become a persisted timeline entry. (The
    step's own `detail` already carries the call's arguments, truncated to the pre-existing
    `_TOOL_DETAIL_CAP` — that bound is what keeps a long write out of the message row, and it
    predates the pane.)
    """
    long_body = "# Goals\n" + ("ship it. " * 500)
    events = await _write_events(_doc_lookup(_writes()), {"path": "a.md", "content": long_body})

    running = next(e for e in events if e.type == "tool")
    assert running.document is not None
    assert running.document["content"] == long_body  # in full, over the wire

    done = events[-1]
    assert done.turn is not None and done.turn.activity is not None
    step = done.turn.activity.steps[0]
    assert step.tool == "write_doc"
    # No `document` on the persisted step, and the body it does mention stays capped.
    assert not hasattr(step, "document")
    assert step.detail is not None and len(step.detail) <= _TOOL_DETAIL_CAP
    assert len(json.dumps(done.turn.activity.model_dump())) < len(long_body)


# ── the live typewriter (#654, ADR-0121) ─────────────────────────────────────
#
# v1 above shows the document once the call *lands*. These are the other half: the same document
# arriving character by character, off the gateway's new streamed tool-call fragments.


class _FragmentGateway:
    """Replays scripted rounds of raw ``StreamEvent``s — deltas, fragments and the result.

    The other fake in this file scripts a round as ``(deltas, result)``, which cannot express a
    tool call *being typed* or the interleaving of that with the answer's own tokens. This one
    hands the loop exactly the event sequence a provider would.
    """

    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
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
        for event in self._rounds.pop(0):
            yield event

    async def supports_tools(self, *_a: Any, **_k: Any) -> bool:
        return True

    async def supports_vision(self, *_a: Any, **_k: Any) -> bool:
        return True


def _typed_call(
    arguments: str, *, chunk: int = 8, tool: str = "write_doc", call_id: str = "c1"
) -> list[StreamEvent]:
    """The events a provider emits while typing one tool call's arguments."""
    pieces = [arguments[i : i + chunk] for i in range(0, len(arguments), chunk)] or [""]
    events = [StreamEvent(tool_call=ToolCallFragment(slot=0, id=call_id, name=tool))]
    events += [
        StreamEvent(tool_call=ToolCallFragment(slot=0, id=call_id, name=tool, arguments=piece))
        for piece in pieces
    ]
    events.append(
        StreamEvent(
            result=ChatResult(model="m", content="", tool_calls=[_tool_call(tool, arguments)])
        )
    )
    return events


async def _typed_events(
    arguments: dict[str, Any],
    *,
    lookup: DocumentToolLookup | None = None,
    chunk: int = 8,
) -> list[AgentEvent]:
    """Run one turn whose single tool call is typed out fragment by fragment."""
    raw = json.dumps(arguments)
    answer = [
        StreamEvent(delta="saved"),
        StreamEvent(result=ChatResult(model="m", content="saved")),
    ]
    gw = _FragmentGateway([_typed_call(raw, chunk=chunk), answer])
    agent = Agent(
        gateway=gw,  # type: ignore[arg-type]
        mcp=_ScribeMcp(),  # type: ignore[arg-type]
        documents=_doc_lookup(_writes(target_arg="path")) if lookup is None else lookup,
    )
    return await _collect(agent, "write it down")


async def test_a_write_types_itself_into_the_pane_as_the_model_writes_it() -> None:
    body = "# Goals\n\nship the typewriter"
    events = await _typed_events({"path": "notes/goals.md", "content": body})

    previews = [e for e in events if e.type == "doc_preview"]
    assert previews, "an annotated call should preview while it is being typed"
    # The deltas concatenate to exactly what the model wrote — nothing lost, nothing doubled.
    assert "".join(e.text or "" for e in previews) == body
    # Every frame names its own document, so a client that joins late still knows what it is.
    for frame in previews:
        assert frame.tool == "write_doc"
        assert frame.preview is not None and frame.preview["module"] == "knowledge"
    assert previews[-1].preview == {"module": "knowledge", "target": "notes/goals.md"}


async def test_the_typewriter_runs_before_the_call_is_executed() -> None:
    events = await _typed_events({"path": "a.md", "content": "body text here"})
    kinds = [e.type for e in events]
    # preview(s) → `running` (the pane's authoritative payload) → `ok` → the answer.
    assert kinds.index("doc_preview") < kinds.index("tool")
    assert [e.status for e in events if e.type == "tool"] == ["running", "ok"]


async def test_the_pane_settles_on_the_parsed_call_not_on_the_preview() -> None:
    # ADR-0101's rule: the pane must never lie about what was written. The preview is a *guess*
    # read out of half-typed JSON; the `tool` frames carry the arguments as actually parsed, and
    # they are what the pane ends on.
    body = 'a "quoted" body\nwith a newline'
    events = await _typed_events({"path": "a.md", "content": body})

    settled = [e for e in events if e.type == "tool"]
    assert [e.document for e in settled] == [
        {"module": "knowledge", "content": body, "target": "a.md", "title": None}
    ] * 2
    assert all(e.document is None for e in events if e.type == "doc_preview")


async def test_a_preview_never_reaches_the_persisted_activity() -> None:
    """ADR-0041, restated for the typewriter: previews ride SSE and stop there.

    v1 keeps a *finished* document out of the timeline; a preview is a stream of them, so the
    same rule matters more, not less. The turn's activity must look exactly as it would have
    without the typewriter.
    """
    body = "# Long\n" + ("every word of this is streamed. " * 200)
    events = await _typed_events({"path": "a.md", "content": body}, chunk=16)

    assert len([e for e in events if e.type == "doc_preview"]) > 0
    done = events[-1]
    assert done.turn is not None
    activity = done.turn.activity
    assert [s.tool for s in activity.steps] == ["write_doc"]  # one step, not one per fragment
    step = activity.steps[0]
    assert not hasattr(step, "preview")
    assert step.detail is not None and len(step.detail) <= _TOOL_DETAIL_CAP
    serialized = json.dumps(activity.model_dump())
    # The body never lands in the row — only the pre-existing, capped call detail does.
    assert len(serialized) < len(body)
    assert "doc_preview" not in serialized


async def test_a_long_write_never_starves_the_chat_deltas() -> None:
    """#541's constraint: the document and the answer share one stream, so bound the document.

    Two things have to hold at once — the answer's tokens go out *immediately and in order*
    (they are never queued behind a document), and the document's own frames are coalesced far
    below the fragment rate so they cannot swamp the connection.
    """
    body = "word " * 4000  # 20k characters, typed in ~2500 fragments
    raw = json.dumps({"path": "a.md", "content": body})
    pieces = [raw[i : i + 8] for i in range(0, len(raw), 8)]
    # Answer tokens scattered through the write, as a model that narrates while it writes.
    marks = {0: "writing", len(pieces) // 2: " it", len(pieces) - 1: " now"}
    chat_tokens = list(marks.values())
    scripted: list[StreamEvent] = [
        StreamEvent(tool_call=ToolCallFragment(slot=0, id="c1", name="write_doc"))
    ]
    for index, piece in enumerate(pieces):
        if (token := marks.get(index)) is not None:
            scripted.append(StreamEvent(delta=token))
        scripted.append(
            StreamEvent(
                tool_call=ToolCallFragment(slot=0, id="c1", name="write_doc", arguments=piece)
            )
        )
    scripted.append(
        StreamEvent(
            result=ChatResult(model="m", content="", tool_calls=[_tool_call("write_doc", raw)])
        )
    )
    gw = _FragmentGateway(
        [
            scripted,
            [StreamEvent(delta="done"), StreamEvent(result=ChatResult(model="m", content="done"))],
        ]
    )
    agent = Agent(
        gateway=gw,  # type: ignore[arg-type]
        mcp=_ScribeMcp(),  # type: ignore[arg-type]
        documents=_doc_lookup(_writes(target_arg="path")),
    )
    events = await _collect(agent, "write a long one")

    stream = [e for e in events if e.type in {"delta", "doc_preview"}]
    previews = [e for e in stream if e.type == "doc_preview"]
    deltas = [e for e in stream if e.type == "delta"]
    # Every answer token arrived, in order, and none of them waited for the document.
    assert [e.text for e in deltas][:3] == chat_tokens
    assert stream[0].type == "delta"  # the first token beat the first preview frame
    kinds = [e.type for e in stream]
    middle = kinds.index("delta", 1)
    assert "doc_preview" in kinds[:middle]  # the document was already flowing…
    assert "doc_preview" in kinds[middle + 1 :]  # …and kept flowing after the token
    # The document was coalesced by a wide margin — orders of magnitude fewer frames than
    # fragments — while still delivering every character.
    assert len(previews) < len(pieces) / 50
    assert "".join(e.text or "" for e in previews) == body


async def test_an_unannotated_call_is_typed_out_in_silence() -> None:
    events = await _typed_events({"path": "a.md", "content": "body"}, lookup=_doc_lookup(None))
    assert not [e for e in events if e.type == "doc_preview"]


async def test_no_document_lookup_means_no_typewriter() -> None:
    raw = json.dumps({"path": "a.md", "content": "body"})
    gw = _FragmentGateway(
        [
            _typed_call(raw),
            [StreamEvent(delta="ok"), StreamEvent(result=ChatResult(model="m", content="ok"))],
        ]
    )
    events = await _collect(Agent(gateway=gw, mcp=_ScribeMcp()), "go")  # type: ignore[arg-type]
    assert not [e for e in events if e.type == "doc_preview"]


async def test_a_body_that_never_starts_opens_no_pane() -> None:
    events = await _typed_events({"path": "a.md", "content": ""})
    assert not [e for e in events if e.type == "doc_preview"]


async def test_a_second_step_does_not_inherit_the_first_steps_preview_state() -> None:
    """Fragment slots restart per gateway call; a fresh tracker per step is what makes that safe.

    Without it, step two's slot 0 would resolve to step one's tool and its body would be
    appended to the previous document's — the pane would show two writes fused into one.
    """
    first = json.dumps({"path": "one.md", "content": "first body"})
    second = json.dumps({"path": "two.md", "content": "second body"})
    gw = _FragmentGateway(
        [
            _typed_call(first, call_id="c1"),
            _typed_call(second, call_id="c2"),
            [StreamEvent(delta="both"), StreamEvent(result=ChatResult(model="m", content="both"))],
        ]
    )
    agent = Agent(
        gateway=gw,  # type: ignore[arg-type]
        mcp=_ScribeMcp(),  # type: ignore[arg-type]
        documents=_doc_lookup(_writes(target_arg="path")),
    )
    events = await _collect(agent, "write two")

    previews = [e for e in events if e.type == "doc_preview"]
    targets = {e.preview["target"] for e in previews if e.preview and "target" in e.preview}
    assert targets == {"one.md", "two.md"}
    bodies = [
        "".join(e.text or "" for e in previews if (e.preview or {}).get("target") == t)
        for t in ("one.md", "two.md")
    ]
    assert bodies == ["first body", "second body"]
