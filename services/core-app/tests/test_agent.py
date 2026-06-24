"""Unit tests for the agent loop — the gateway and MCP host are mocked (no network)."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import Attachment, EntityRef, tool_envelope
from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.prefs import LlmPrefsStore


async def _fresh_prefs() -> LlmPrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prefs = LlmPrefsStore(engine)
    await prefs.init()
    return prefs


class _FakeGateway:
    def __init__(self, results: list[ChatResult], *, supports_tools: bool = True) -> None:
        self._results = list(results)
        self.calls: list[list[ChatMessage]] = []
        self.tools_seen: list[Any] = []
        self._supports_tools = supports_tools

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: Any = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        return self._results.pop(0)

    async def supports_tools(self, *_a: Any, **_k: Any) -> bool:
        return self._supports_tools


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

    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
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


async def test_agent_skips_tools_when_the_model_cannot_use_them() -> None:
    # A model that can't call tools must be offered none — passing tools makes the runtime
    # error. The turn falls back to a plain text answer even though MCP has tools available.
    gw = _FakeGateway([ChatResult(model="m", content="just chatting")], supports_tools=False)
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"}, outputs={"echo": "x"})
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="hi")])
    assert turn.content == "just chatting"
    assert turn.tools_used == []
    assert gw.tools_seen == [None]  # tools never offered, despite specs existing


async def test_agent_offers_tools_when_the_model_supports_them() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="hi")], supports_tools=True)
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"})
    await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="hi")])
    assert gw.tools_seen[0] == [_echo_spec()]  # the tool specs were offered


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


async def test_agent_max_steps_resolved_from_prefs_at_runtime() -> None:
    # The stored pref (1) overrides the constructor default (4) per turn — no restart needed.
    store = await _fresh_prefs()
    await store.set_agent_max_steps("local", 1)
    results = [
        ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
        ChatResult(model="m", content="final"),
    ]
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"}, outputs={"echo": "x"})
    turn = await Agent(gateway=_FakeGateway(results), mcp=mcp, max_steps=4, prefs=store).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == "max_steps"  # stopped after one round despite the default of 4
    assert turn.tools_used == ["echo"]


async def test_agent_max_steps_falls_back_to_constructor_default() -> None:
    # With no stored pref, the constructor default (4) applies — the same two results
    # answer on round 2, well under the bound.
    store = await _fresh_prefs()
    results = [
        ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
        ChatResult(model="m", content="final"),
    ]
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"}, outputs={"echo": "x"})
    turn = await Agent(gateway=_FakeGateway(results), mcp=mcp, max_steps=4, prefs=store).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == "completed"


async def test_agent_handles_tool_error() -> None:
    class _FailingMcp(_FakeMcp):
        async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
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
        self.remembered_refs: list[dict[str, Any]] = []  # refs of the last remember()
        self.remembered_attachments: list[dict[str, Any]] = []  # attachments of the last remember()
        self.remembered_activity: dict[str, Any] | None = None  # activity of the last remember()

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        if self._fail:
            raise RuntimeError("qdrant down")
        return list(self._recalled)

    async def history(self, *, tenant: str, session_id: str) -> list[ChatMessage]:
        if self._fail:
            raise RuntimeError("db down")
        return list(self._history)

    async def remember(
        self,
        *,
        tenant: str,
        session_id: str,
        role: str,
        content: str,
        entity_refs: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        activity: dict[str, Any] | None = None,
    ) -> None:
        if self._fail:
            raise RuntimeError("db down")
        self.remembered.append((role, content))
        self.remembered_refs = entity_refs or []
        self.remembered_attachments = attachments or []
        self.remembered_activity = activity


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


# ── entity references from tool envelopes (ADR-0019) ─────────────────────────────


def _ref() -> EntityRef:
    return EntityRef(ref_id="e1", module="calendar", kind="event", title="Standup")


async def test_agent_lifts_entity_refs_from_a_tool_envelope() -> None:
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("make_event", "{}")]),
            ChatResult(model="m", content="done"),
        ]
    )
    mcp = _FakeMcp(
        specs=[_echo_spec()],
        route={"make_event": "u"},
        outputs={"make_event": tool_envelope("Created the event.", [_ref()])},
    )
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="schedule it")])

    assert [r.ref_id for r in turn.entity_refs] == ["e1"]
    # the envelope's *text* — not its JSON — is fed back to the model
    assert any(m.role == "tool" and m.content == "Created the event." for m in gw.calls[1])


async def test_agent_dedupes_entity_refs_across_tool_calls() -> None:
    env = tool_envelope("ok", [_ref()])
    gw = _FakeGateway(
        [
            ChatResult(
                model="m",
                content="",
                tool_calls=[_tool_call("a", "{}", "c1"), _tool_call("b", "{}", "c2")],
            ),
            ChatResult(model="m", content="done"),
        ]
    )
    mcp = _FakeMcp(specs=[_echo_spec()], route={"a": "u", "b": "u"}, outputs={"a": env, "b": env})
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="go")])
    assert [r.ref_id for r in turn.entity_refs] == ["e1"]


async def test_agent_plain_tool_output_yields_no_refs() -> None:
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="ok"),
        ]
    )
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"}, outputs={"echo": "just text"})
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="go")])
    assert turn.entity_refs == []


async def test_agent_persists_entity_refs_with_the_answer() -> None:
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("make_event", "{}")]),
            ChatResult(model="m", content="done"),
        ]
    )
    mcp = _FakeMcp(
        specs=[_echo_spec()],
        route={"make_event": "u"},
        outputs={"make_event": tool_envelope("Created.", [_ref()])},
    )
    memory = _FakeMemory()
    await Agent(gateway=gw, mcp=mcp, memory=memory).run(
        [ChatMessage(role="user", content="go")], session_id="s1"
    )
    assert memory.remembered_refs == [_ref().model_dump()]


# ── attachments expanded into context (ADR-0019) ─────────────────────────────────


class _FakeExpander:
    def __init__(self, text: str = "the attached notes") -> None:
        self._text = text
        self.calls: list[list[Attachment]] = []

    async def expand(self, attachments: list[Attachment], *, tenant: str) -> str:
        self.calls.append(attachments)
        return self._text


async def test_agent_injects_attachment_context() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="answer")])
    expander = _FakeExpander("CONTENTS OF notes.txt")
    msg = ChatMessage(
        role="user",
        content="summarize",
        attachments=[Attachment(att_id="a1", source="file", title="notes.txt")],
    )
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), attachments=expander).run([msg])

    assert turn.content == "answer"
    assert expander.calls and expander.calls[0][0].att_id == "a1"
    # the expanded text reached the model as a leading system message
    assert any(
        m.role == "system" and "CONTENTS OF notes.txt" in (m.content or "") for m in gw.calls[0]
    )


async def test_agent_without_attachments_skips_expansion() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="ok")])
    expander = _FakeExpander()
    await Agent(gateway=gw, mcp=_FakeMcp(), attachments=expander).run(
        [ChatMessage(role="user", content="hi")]
    )
    assert expander.calls == []


async def test_agent_attachment_failure_degrades_to_plain_turn() -> None:
    class _BoomExpander:
        async def expand(self, attachments: list[Attachment], *, tenant: str) -> str:
            raise RuntimeError("storage down")

    gw = _FakeGateway([ChatResult(model="m", content="still works")])
    msg = ChatMessage(
        role="user",
        content="summarize",
        attachments=[Attachment(att_id="a1", source="file", title="notes.txt")],
    )
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), attachments=_BoomExpander()).run([msg])
    assert turn.content == "still works"
    # no system context was injected — the model just saw the user message
    assert [m.content for m in gw.calls[0]] == ["summarize"]


# ── background fact extraction (ADR-0045) ────────────────────────────────────────


class _FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []  # tenant, user_text, assistant_text

    async def extract(self, *, tenant: str, user_text: str, assistant_text: str) -> list[Any]:
        self.calls.append((tenant, user_text, assistant_text))
        return []


async def _drain(extractor: _FakeExtractor) -> None:
    """Yield to the loop until the scheduled background task has run (deterministic)."""
    for _ in range(5):
        if extractor.calls:
            return
        await asyncio.sleep(0)


async def test_agent_schedules_fact_extraction_after_a_turn() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="Nice to meet you, Sam.")])
    extractor = _FakeExtractor()
    agent = Agent(gateway=gw, mcp=_FakeMcp(), extractor=extractor)
    await agent.run([ChatMessage(role="user", content="My name is Sam.")])
    await _drain(extractor)
    assert extractor.calls == [("local", "My name is Sam.", "Nice to meet you, Sam.")]


async def test_agent_skips_extraction_without_an_answer() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="")])
    extractor = _FakeExtractor()
    await Agent(gateway=gw, mcp=_FakeMcp(), extractor=extractor).run(
        [ChatMessage(role="user", content="hi")]
    )
    await _drain(extractor)
    assert extractor.calls == []
