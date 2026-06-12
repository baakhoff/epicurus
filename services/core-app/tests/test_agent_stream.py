"""Unit tests for the streaming agent loop — gateway and MCP host are faked."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from epicurus_core_app.agent.agent import Agent, AgentEvent
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


class _FakeMcp:
    def __init__(self, outputs: dict[str, str] | None = None, fail: bool = False) -> None:
        self._outputs = outputs or {}
        self._fail = fail

    async def discover(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs = [{"type": "function", "function": {"name": "echo"}}]
        return specs, {"echo": "http://echo:8080/mcp"}

    async def call(self, name: str, arguments: dict[str, Any], url: str) -> str:
        if self._fail:
            raise RuntimeError("boom")
        return self._outputs.get(name, "out")


def _tool_call(name: str = "echo") -> dict[str, Any]:
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}


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
        async def stream_chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            raise RuntimeError("paused")
            yield StreamEvent()  # pragma: no cover - makes this an async generator

    events = await _collect(Agent(gateway=_Exploding(), mcp=_FakeMcp()), "hi")  # type: ignore[arg-type]
    assert [e.type for e in events] == ["error"]
    assert events[0].detail == "paused"


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
