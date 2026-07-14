"""Unit tests for the streaming agent loop — gateway and MCP host are faked."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import Attachment, draft_review
from epicurus_core_app.agent.agent import (
    _ANSWER_NUDGE,
    _EMPTY_ANSWER_FALLBACK,
    _REPEAT_NUDGE,
    _STOPPED_REPEAT_CALL,
    _STOPPED_TOOL_ERRORS,
    _STOPPED_UNSUPPORTED_MEDIA,
    _STREAM_STALLED_MESSAGE,
    _VISION_UNSUPPORTED_MESSAGE,
    Agent,
    AgentEvent,
)
from epicurus_core_app.agent.attachments import ExpandedAttachments, ImagePart
from epicurus_core_app.agent.mcp_host import ToolCallError
from epicurus_core_app.agent.pending_drafts import PendingDraftStore
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.llm.models import ChatMessage, ChatResult, StreamEvent


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


async def test_stream_tool_reported_failure_shows_error_status() -> None:
    # A tool that ran but reported failure (MCP isError → ToolCallError, #435/#440) must stream
    # an `error` status and persist an `error` step — not the green "ok" a text-prefix check
    # gave it, since the tool's own message (fed to the model verbatim) need not begin with
    # "error:". This is the SSE status the web timeline renders (red X vs. green check).
    class _ErrorMcp(_FakeMcp):
        async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
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

    async def discover(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs = [{"type": "function", "function": {"name": "mail_send"}}]
        return specs, {"mail_send": "http://mail:8080/mcp"}

    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
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
        m.role == "tool" and m.name == "mail_send" and (m.content or "").startswith("error:")
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


# ── Loop hygiene: same rules as run(), applied to run_stream (#524) ───────────


class _CountingMcp(_FakeMcp):
    """A faked MCP that records each tool invocation (and optionally fails every call)."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.calls_made: list[dict[str, Any]] = []

    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
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
