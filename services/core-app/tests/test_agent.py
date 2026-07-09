"""Unit tests for the agent loop — the gateway and MCP host are mocked (no network)."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from epicurus_core import LIST_CAP, Attachment, EntityRef, draft_review, tool_envelope
from epicurus_core_app.agent.agent import _ANSWER_NUDGE, _EMPTY_ANSWER_FALLBACK, Agent
from epicurus_core_app.agent.instructions import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AgentInstructionsStore,
)
from epicurus_core_app.agent.mcp_host import ToolCallError
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.prefs import LlmPrefsStore


async def _fresh_instructions(default: str = DEFAULT_AGENT_INSTRUCTIONS) -> AgentInstructionsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = AgentInstructionsStore(engine, default=default)
    await store.init()
    return store


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


async def test_agent_non_streaming_does_not_send_a_composed_draft() -> None:
    # A compose tool (mail_send) returns a DraftReview on the non-streaming path (POST /chat, the
    # messaging bridge) — which has no split-pane to Confirm/Decline in. The loop must NOT feed the
    # raw envelope back (the model would misreport "sent"): it hands the model an honest
    # "not sent from this channel" error, and nothing is transmitted (ADR-0085, #563).
    draft = draft_review(
        kind="mail", module="mail", draft={"to": "b@x.com", "subject": "Hi", "body": "yo"}
    )
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("mail_send", "{}")]),
            ChatResult(model="m", content="I couldn't send that from here."),
        ]
    )
    mcp = _FakeMcp(
        specs=[{"type": "function", "function": {"name": "mail_send"}}],
        route={"mail_send": "http://mail:8080/mcp"},
        outputs={"mail_send": draft},
    )
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="email bob")])

    tool_msgs = [m for m in gw.calls[1] if m.role == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0].content or ""
    assert content.startswith("error:") and "not sent" in content.lower()
    assert '"kind"' not in content  # the model never saw the raw draft envelope
    # The step is recorded as not-ok in the activity timeline.
    assert [s.status for s in turn.activity.steps] == ["error"]


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


async def test_agent_feeds_tool_reported_failure_text_to_the_model() -> None:
    # A tool that ran but reported failure (MCP isError → ToolCallError, #435) hands the
    # model the tool's own message — the exact text it received before the host raised —
    # so the model can react (retry, apologise, ask) without the turn crashing.
    class _ErrorMcp(_FakeMcp):
        async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
            raise ToolCallError("Error executing tool echo: event 'e1' not found")

    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="that event does not exist"),
        ]
    )
    mcp = _ErrorMcp(specs=[_echo_spec()], route={"echo": "u"})
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="go")])

    assert turn.content == "that event does not exist"
    fed_back = next(m.content for m in gw.calls[1] if m.role == "tool")
    assert fed_back == "Error executing tool echo: event 'e1' not found"


async def test_agent_marks_tool_reported_failure_as_an_error_step() -> None:
    # A tool that ran but reported failure (ToolCallError, #435/#440) must show as a failed
    # step in the activity timeline — a red "error", not the green "ok" a text-prefix check
    # gave it. The tool's own message (fed to the model verbatim) need not begin with "error:",
    # so the status is classified from whether the call raised, not from the returned text.
    class _ErrorMcp(_FakeMcp):
        async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
            raise ToolCallError("Error executing tool echo: event 'e1' not found")

    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="that event does not exist"),
        ]
    )
    mcp = _ErrorMcp(specs=[_echo_spec()], route={"echo": "u"})
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="go")])

    # The failed call is flagged as an error step (not a green "ok")…
    assert [s.status for s in turn.activity.steps] == ["error"]
    # …while the model still receives the tool's raw message, with no "error:" prefix added.
    fed_back = next(m.content for m in gw.calls[1] if m.role == "tool")
    assert fed_back == "Error executing tool echo: event 'e1' not found"


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
    # the envelope's *text* — not its JSON — is fed back to the model, now with the refs' ids
    # appended so the model can act on the entities (#449)
    tool_msg = next(m for m in gw.calls[1] if m.role == "tool")
    assert tool_msg.content is not None
    assert tool_msg.content.startswith("Created the event.")
    assert "e1" in tool_msg.content  # the ref id now reaches the model


async def test_agent_feeds_entity_ref_ids_to_the_model() -> None:
    # A module lists entities with an envelope whose text names them without ids, but whose refs
    # carry the ids (the calendar_list_events shape). The agent appends each ref's id to the text
    # the model sees, so a "list then edit that one" flow has an id to pass — the #449 fix, applied
    # once in the core for every module with refs rather than per-module.
    listing = tool_envelope(
        "Found 2 event(s):\n- Standup (Mon 9am)\n- Retro (Fri 3pm)",
        [
            EntityRef(ref_id="evt_1", module="calendar", kind="event", title="Standup"),
            EntityRef(ref_id="evt_2", module="calendar", kind="event", title="Retro"),
        ],
    )
    gw = _FakeGateway(
        [
            ChatResult(
                model="m", content="", tool_calls=[_tool_call("calendar_list_events", "{}")]
            ),
            ChatResult(model="m", content="here they are"),
        ]
    )
    mcp = _FakeMcp(
        specs=[_echo_spec()],
        route={"calendar_list_events": "u"},
        outputs={"calendar_list_events": listing},
    )
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="my events?")])

    tool_msg = next(m for m in gw.calls[1] if m.role == "tool")
    assert tool_msg.content is not None
    # the original listing text is preserved…
    assert tool_msg.content.startswith("Found 2 event(s):")
    # …and each event's id is now available to the model, paired with its title
    assert "evt_1" in tool_msg.content and "evt_2" in tool_msg.content
    assert "Standup" in tool_msg.content and "Retro" in tool_msg.content
    # the UI still receives the refs as chips (unchanged)
    assert [r.ref_id for r in turn.entity_refs] == ["evt_1", "evt_2"]


async def test_agent_caps_the_entity_ref_id_block_but_not_the_ui_chips() -> None:
    # A large ref list (RRULE-expanded calendar events, a wide search) roughly doubles its
    # context cost once every id is echoed into the model-facing block too — so the block
    # itself is capped at LIST_CAP (#468), independent of the full ref list the UI's chips
    # still get.
    total = LIST_CAP + 7
    refs = [
        EntityRef(ref_id=f"evt_{i}", module="calendar", kind="event", title=f"Event {i}")
        for i in range(total)
    ]
    listing = tool_envelope(f"Found {total} event(s):\n...", refs)
    gw = _FakeGateway(
        [
            ChatResult(
                model="m", content="", tool_calls=[_tool_call("calendar_list_events", "{}")]
            ),
            ChatResult(model="m", content="here they are"),
        ]
    )
    mcp = _FakeMcp(
        specs=[_echo_spec()],
        route={"calendar_list_events": "u"},
        outputs={"calendar_list_events": listing},
    )
    with capture_logs() as logs:
        turn = await Agent(gateway=gw, mcp=mcp).run(
            [ChatMessage(role="user", content="my events?")], tenant_id="tenant-a"
        )

    tool_msg = next(m for m in gw.calls[1] if m.role == "tool")
    assert tool_msg.content is not None
    # Only the first LIST_CAP ids reach the model...
    assert f"evt_{LIST_CAP - 1}" in tool_msg.content
    assert f"evt_{LIST_CAP}" not in tool_msg.content
    assert f"showing {LIST_CAP} of {total}" in tool_msg.content
    # ...but the UI's chips still carry every ref, uncapped.
    assert len(turn.entity_refs) == total

    truncated_event = "entity-ref id block truncated for the model"
    truncation_logs = [e for e in logs if e["event"] == truncated_event]
    assert truncation_logs
    assert truncation_logs[0]["total"] == total
    assert truncation_logs[0]["shown"] == LIST_CAP
    assert truncation_logs[0]["tenant_id"] == "tenant-a"


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


async def test_agent_empty_entity_refs_envelope_yields_no_refs_or_appended_block() -> None:
    # Distinct from the plain-string case above (#466): a *valid* ToolEnvelope whose
    # entity_refs is explicitly [] must take the same early return as no envelope at all —
    # no chips, and critically no "Referenced items" block appended to what the model sees
    # (that block is only ever built from a non-empty ref list, #449).
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="ok"),
        ]
    )
    mcp = _FakeMcp(
        specs=[_echo_spec()],
        route={"echo": "u"},
        outputs={"echo": tool_envelope("just an envelope, no refs")},
    )
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="go")])

    assert turn.entity_refs == []
    tool_msg = next(m for m in gw.calls[1] if m.role == "tool")
    assert tool_msg.content == "just an envelope, no refs"  # unchanged — nothing appended


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
    # The model produces no answer (blank, even after the nudge) → the turn falls back to a
    # canned message, and extraction is skipped: there's nothing to learn from a non-answer.
    gw = _FakeGateway([ChatResult(model="m", content=""), ChatResult(model="m", content="")])
    extractor = _FakeExtractor()
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), extractor=extractor).run(
        [ChatMessage(role="user", content="hi")]
    )
    await _drain(extractor)
    assert turn.content == _EMPTY_ANSWER_FALLBACK
    assert extractor.calls == []


# ── deferred (nightly) extraction + bounded recall (ADR-0051) ─────────────────────


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, str]] = []  # tenant, user_text, assistant_text

    async def enqueue(self, *, tenant: str, user_text: str, assistant_text: str) -> int:
        self.enqueued.append((tenant, user_text, assistant_text))
        return len(self.enqueued)


async def _settle() -> None:
    """Yield to the loop a few times so a scheduled background task runs (deterministic)."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_agent_defers_extraction_by_enqueuing_the_exchange() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="Nice to meet you, Sam.")])
    queue = _FakeQueue()
    extractor = _FakeExtractor()
    # Default mode is deferred: the exchange is queued for the nightly runner, not distilled now.
    agent = Agent(gateway=gw, mcp=_FakeMcp(), extractor=extractor, queue=queue)  # type: ignore[arg-type]
    await agent.run([ChatMessage(role="user", content="My name is Sam.")])
    await _settle()
    assert queue.enqueued == [("local", "My name is Sam.", "Nice to meet you, Sam.")]
    assert extractor.calls == []  # the extractor is NOT called on the response path


async def test_agent_immediate_mode_extracts_even_with_a_queue() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="Hi Sam.")])
    queue = _FakeQueue()
    extractor = _FakeExtractor()
    agent = Agent(
        gateway=gw,
        mcp=_FakeMcp(),
        extractor=extractor,
        queue=queue,  # type: ignore[arg-type]
        defer_extraction=False,
    )
    await agent.run([ChatMessage(role="user", content="I'm Sam.")])
    await _drain(extractor)
    assert extractor.calls == [("local", "I'm Sam.", "Hi Sam.")]
    assert queue.enqueued == []  # immediate mode never defers


class _SlowMemory(_FakeMemory):
    """A memory whose recall hangs longer than any sane budget."""

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        await asyncio.sleep(1)
        return ["should not appear"]


async def test_agent_recall_timeout_degrades_to_no_recall() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="answer")])
    memory = _SlowMemory(recalled=["should not appear"])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=memory, recall_timeout_s=0.01)
    turn = await agent.run([ChatMessage(role="user", content="hi")], session_id="s1")
    assert turn.content == "answer"
    # recall timed out → no recalled-context system message reached the model …
    assert all("should not appear" not in (m.content or "") for m in gw.calls[0])
    # … but history assembly and persistence still happened (the turn isn't lost)
    assert ("user", "hi") in memory.remembered


class _RecallErrorMemory(_FakeMemory):
    """Recall raises a backend error (not a timeout); history and persistence still work."""

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        raise RuntimeError("qdrant unreachable")


async def test_agent_recall_timeout_logs_a_named_timeout() -> None:
    # A slow embedder trips the budget: the turn proceeds, and the log says *timed out* (naming
    # the budget) rather than the old blank `error=` that `str(TimeoutError())` produced.
    gw = _FakeGateway([ChatResult(model="m", content="answer")])
    agent = Agent(
        gateway=gw, mcp=_FakeMcp(), memory=_SlowMemory(recalled=["x"]), recall_timeout_s=0.01
    )
    with capture_logs() as logs:
        await agent.run([ChatMessage(role="user", content="hi")], session_id="s1")
    timeout_logs = [e for e in logs if e["event"] == "recall skipped: embed timed out"]
    assert timeout_logs and timeout_logs[0]["timeout_s"] == 0.01


async def test_agent_recall_backend_error_is_logged_distinctly() -> None:
    # A backend failure (not a timeout) degrades the same way but logs the exception type, so the
    # operator can tell a broken embedder/Qdrant from a too-tight budget.
    gw = _FakeGateway([ChatResult(model="m", content="answer")])
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=_RecallErrorMemory(), recall_timeout_s=5)
    with capture_logs() as logs:
        turn = await agent.run([ChatMessage(role="user", content="hi")], session_id="s1")
    assert turn.content == "answer"  # still degrades to no recall, never blocks
    error_logs = [e for e in logs if e["event"] == "recall skipped: backend error"]
    assert error_logs and error_logs[0]["error_type"] == "RuntimeError"


async def test_agent_blank_step_is_nudged_into_an_answer() -> None:
    # A reasoning model thinks but returns no answer/tool the first time; the loop nudges it once
    # and it answers on the retry — instead of ending the turn empty (the silent "stop").
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", reasoning="I should write a roadmap…"),
            ChatResult(model="m", content="here is the roadmap"),
        ]
    )
    turn = await Agent(gateway=gw, mcp=_FakeMcp()).run(
        [ChatMessage(role="user", content="make a roadmap")]
    )
    assert turn.content == "here is the roadmap"
    assert turn.stopped == "completed"
    assert any(m.role == "user" and m.content == _ANSWER_NUDGE for m in gw.calls[1])


async def test_agent_never_returns_an_empty_turn() -> None:
    # The model says nothing, even after the nudge: the turn carries a clear fallback message,
    # never empty content (which would render as a silent stop).
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", reasoning="hmm"),
            ChatResult(model="m", content="   "),
        ]
    )
    turn = await Agent(gateway=gw, mcp=_FakeMcp()).run([ChatMessage(role="user", content="hi")])
    assert turn.content == _EMPTY_ANSWER_FALLBACK
    assert turn.stopped == "completed"


async def test_agent_blank_final_answer_at_max_steps_falls_back() -> None:
    # Tools every round, then the forced final (no-tools) answer is also blank → fallback, still
    # flagged stopped="max_steps".
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content=""),
        ]
    )
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"}, outputs={"echo": "x"})
    turn = await Agent(gateway=gw, mcp=mcp, max_steps=1).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == "max_steps"
    assert turn.content == _EMPTY_ANSWER_FALLBACK


# ── base system prompt (#497, ADR-0083) ───────────────────────────────────────


async def test_base_prompt_leads_the_turn() -> None:
    """The resolved prompt is the FIRST message the model sees — even with no memory/session
    (the headless path), which takes the early return in ``_assemble``."""
    gw = _FakeGateway([ChatResult(model="m", content="ok")])
    store = await _fresh_instructions(default="You are epsilon.")
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), instructions=store).run(
        [ChatMessage(role="user", content="hi")]
    )
    assert turn.content == "ok"
    first = gw.calls[0][0]
    assert first.role == "system"
    assert first.content == "You are epsilon."


async def test_no_base_prompt_when_instructions_unset() -> None:
    """Backward-compat: with no instructions store the agent runs with no base prompt, exactly
    as it did before #497 — the first message is the user's, not a system prompt."""
    gw = _FakeGateway([ChatResult(model="m", content="ok")])
    await Agent(gateway=gw, mcp=_FakeMcp()).run([ChatMessage(role="user", content="hi")])
    assert gw.calls[0][0].role == "user"


async def test_custom_prompt_overrides_default_and_leads() -> None:
    """An operator edit replaces the default and still leads the turn (resolved per turn)."""
    gw = _FakeGateway([ChatResult(model="m", content="ok")])
    store = await _fresh_instructions(default="DEFAULT")
    await store.set_instructions("local", "CUSTOM PROMPT")  # "local" is the agent's default tenant
    await Agent(gateway=gw, mcp=_FakeMcp(), instructions=store).run(
        [ChatMessage(role="user", content="hi")]
    )
    first = gw.calls[0][0]
    assert first.role == "system"
    assert first.content == "CUSTOM PROMPT"
