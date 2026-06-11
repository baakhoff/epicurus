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


class _FakeMemory:
    def __init__(
        self,
        *,
        recalled: list[str] | None = None,
        history: list[ChatMessage] | None = None,
        fail: bool = False,
    ) -> None:
        self._recalled = recalled or []
        self._history = history or []
        self._fail = fail
        self.remembered: list[tuple[str, str]] = []  # (role, content)

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        if self._fail:
            raise RuntimeError("qdrant down")
        return list(self._recalled)

    async def history(self, *, tenant: str, session_id: str) -> list[ChatMessage]:
        if self._fail:
            raise RuntimeError("db down")
        return list(self._history)

    async def remember(self, *, tenant: str, session_id: str, role: str, content: str) -> None:
        if self._fail:
            raise RuntimeError("db down")
        self.remembered.append((role, content))


async def test_agent_uses_memory_when_session_given() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="answer")])
    memory = _FakeMemory(
        recalled=["the user's name is Sam"],
        history=[
            ChatMessage(role="user", content="earlier question"),
            ChatMessage(role="assistant", content="earlier answer"),
        ],
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=memory)
    turn = await agent.run([ChatMessage(role="user", content="what's my name?")], session_id="s1")

    assert turn.content == "answer"
    # the model saw recalled context + prior history + the new message, in order
    sent = gw.calls[0]
    assert sent[0].role == "system" and "Sam" in (sent[0].content or "")
    assert [m.content for m in sent if m.role == "user"] == ["earlier question", "what's my name?"]
    assert any(m.role == "assistant" and m.content == "earlier answer" for m in sent)
    # both the new user turn and the answer were persisted
    assert memory.remembered == [("user", "what's my name?"), ("assistant", "answer")]


async def test_agent_without_session_skips_memory() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    memory = _FakeMemory(recalled=["should not appear"])
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), memory=memory).run(
        [ChatMessage(role="user", content="hi")]
    )
    assert turn.content == "hi"
    assert memory.remembered == []
    assert all("should not appear" not in (m.content or "") for m in gw.calls[0])


async def test_agent_memory_failure_degrades_to_plain_turn() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="still works")])
    memory = _FakeMemory(fail=True)
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), memory=memory).run(
        [ChatMessage(role="user", content="hi")], session_id="s1"
    )
    # memory blew up on read and write, but the chat still answered
    assert turn.content == "still works"
    assert turn.stopped == "completed"
    # no memory context was prepended — the model just got the user message
    assert [m.content for m in gw.calls[0]] == ["hi"]
