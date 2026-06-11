"""Unit tests for the agent loop — the gateway and MCP host are mocked (no network)."""

from __future__ import annotations

from typing import Any

from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.llm.models import ChatMessage, ChatResult


class _FakeGateway:
    def __init__(self, results: list[ChatResult]) -> None:
        self._results = list(results)
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: Any = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        self.calls.append(list(messages))
        return self._results.pop(0)


class _FakeMcp:
    def __init__(
        self,
        specs: list[dict[str, Any]] | None = None,
        route: dict[str, str] | None = None,
        outputs: dict[str, str] | None = None,
    ) -> None:
        self._specs = specs or []
        self._route = route or {}
        self._outputs = outputs or {}
        self.called: list[tuple[str, dict[str, Any]]] = []

    async def discover(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        return self._specs, self._route

    async def call(self, name: str, arguments: dict[str, Any], url: str) -> str:
        self.called.append((name, arguments))
        return self._outputs.get(name, "tool-output")


def _echo_spec() -> dict[str, Any]:
    return {"type": "function", "function": {"name": "echo", "description": "echo"}}


def _tool_call(name: str, args_json: str, call_id: str = "c1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args_json}}


async def test_agent_answers_without_tools() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="hello")])
    turn = await Agent(gateway=gw, mcp=_FakeMcp()).run([ChatMessage(role="user", content="hi")])
    assert turn.content == "hello"
    assert turn.stopped == "completed"
    assert turn.tools_used == []


async def test_agent_calls_tool_then_answers() -> None:
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"message": "hi"}')]),
            ChatResult(model="m", content="the echo said hi"),
        ]
    )
    mcp = _FakeMcp(
        specs=[_echo_spec()], route={"echo": "http://echo:8080/mcp"}, outputs={"echo": "hi"}
    )
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="echo hi")])

    assert mcp.called == [("echo", {"message": "hi"})]
    assert turn.tools_used == ["echo"]
    assert turn.content == "the echo said hi"
    assert turn.stopped == "completed"
    # the tool result was fed back to the model on the second call
    assert any(m.role == "tool" and m.content == "hi" for m in gw.calls[1])


async def test_agent_stops_at_max_steps() -> None:
    results = [
        ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
        ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
        ChatResult(model="m", content="final"),
    ]
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"}, outputs={"echo": "x"})
    turn = await Agent(gateway=_FakeGateway(results), mcp=mcp, max_steps=2).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == "max_steps"
    assert turn.content == "final"


async def test_agent_handles_tool_error() -> None:
    class _FailingMcp(_FakeMcp):
        async def call(self, name: str, arguments: dict[str, Any], url: str) -> str:
            raise RuntimeError("boom")

    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="recovered"),
        ]
    )
    mcp = _FailingMcp(specs=[_echo_spec()], route={"echo": "u"})
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="go")])

    assert turn.content == "recovered"
    assert any(m.role == "tool" and "boom" in (m.content or "") for m in gw.calls[1])
