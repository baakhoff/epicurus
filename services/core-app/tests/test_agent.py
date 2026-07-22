"""Unit tests for the agent loop — the gateway and MCP host are mocked (no network)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from epicurus_core import LIST_CAP, Attachment, EntityRef, draft_review, tool_envelope
from epicurus_core_app.agent.agent import (
    _ANSWER_NUDGE,
    _EMPTY_ANSWER_FALLBACK,
    _MAX_CONSECUTIVE_TOOL_ERRORS,
    _REPEAT_NUDGE,
    _STOPPED_REPEAT_CALL,
    _STOPPED_TOOL_ERRORS,
    _STOPPED_UNSUPPORTED_MEDIA,
    _VISION_UNSUPPORTED_MESSAGE,
    Agent,
    _canonical_calls,
    _LoopGuard,
)
from epicurus_core_app.agent.attachments import ExpandedAttachments, ImagePart
from epicurus_core_app.agent.instructions import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AgentInstructionsStore,
)
from epicurus_core_app.agent.mcp_host import ToolCallError
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.memory.profile import StandingProfile


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
    def __init__(
        self,
        results: list[ChatResult],
        *,
        supports_tools: bool = True,
        supports_vision: bool = True,
    ) -> None:
        self._results = list(results)
        self.calls: list[list[ChatMessage]] = []
        self.tools_seen: list[Any] = []
        self.automation_ids: list[str | None] = []
        self._supports_tools = supports_tools
        self._supports_vision = supports_vision

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: Any = None,
        tenant_id: str | None = None,
        automation_id: str | None = None,
    ) -> ChatResult:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        # Recorded so a test can assert the dual metering attribution reaches the gateway
        # (ADR-0105); None on every ordinary turn.
        self.automation_ids.append(automation_id)
        return self._results.pop(0)

    async def supports_tools(self, *_a: Any, **_k: Any) -> bool:
        return self._supports_tools

    async def supports_vision(self, *_a: Any, **_k: Any) -> bool:
        return self._supports_vision


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
        self.allow_seen: list[frozenset[str] | None] = []

    async def discover(
        self, *, allow: frozenset[str] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        # Recorded so a test can assert an automation's autonomy allowance reaches the
        # tool surface (ADR-0105). The real filtering is tested against McpHost itself.
        self.allow_seen.append(allow)
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


# ── automations: the dial and the metering reach the turn (ADR-0105) ─────────


async def test_an_ordinary_turn_applies_no_dial_and_no_attribution() -> None:
    # Everything about automations is opt-in: a normal turn passes neither, so the tool
    # surface is unfiltered and the usage event carries no automation.
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    mcp = _FakeMcp()
    await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="hi")])
    assert mcp.allow_seen == [None]
    assert gw.automation_ids == [None]


async def test_a_turns_allowance_reaches_the_tool_surface() -> None:
    # The dial is enforced where the tools are assembled, not in the prompt.
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    mcp = _FakeMcp()
    await Agent(gateway=gw, mcp=mcp).run(
        [ChatMessage(role="user", content="hi")], allow=frozenset({"read"})
    )
    assert mcp.allow_seen == [frozenset({"read"})]


async def test_the_automation_attribution_reaches_the_gateway() -> None:
    # The dual metering point: every gateway call a run makes is attributed to it, so the
    # usage event names the tenant *and* which automation spent it.
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    await Agent(gateway=gw, mcp=_FakeMcp()).run(
        [ChatMessage(role="user", content="hi")], automation_id="auto-1"
    )
    assert gw.automation_ids == ["auto-1"]


async def test_a_turn_reports_what_it_cost() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="hi", prompt_tokens=11, completion_tokens=4)])
    turn = await Agent(gateway=gw, mcp=_FakeMcp()).run([ChatMessage(role="user", content="hi")])
    assert turn.usage.prompt_tokens == 11
    assert turn.usage.completion_tokens == 4
    assert turn.usage.steps == 1


async def test_usage_is_summed_across_a_multi_step_turn() -> None:
    # A turn is one *or more* completions — every tool round is another. The ledger wants
    # the total, not the last one.
    gw = _FakeGateway(
        [
            ChatResult(
                model="m",
                content="",
                tool_calls=[_tool_call("t", "{}")],
                prompt_tokens=10,
                completion_tokens=2,
            ),
            ChatResult(model="m", content="done", prompt_tokens=20, completion_tokens=3),
        ]
    )
    mcp = _FakeMcp(specs=[{"type": "function", "function": {"name": "t"}}], route={"t": "u"})
    turn = await Agent(gateway=gw, mcp=mcp).run([ChatMessage(role="user", content="hi")])
    assert turn.usage.prompt_tokens == 30
    assert turn.usage.completion_tokens == 5
    assert turn.usage.steps == 2


async def test_unreported_usage_stays_none_rather_than_zero() -> None:
    # A provider that reports no usage must read as "unknown", not as "free" — reporting 0
    # would quietly understate every bill that depends on it.
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    turn = await Agent(gateway=gw, mcp=_FakeMcp()).run([ChatMessage(role="user", content="hi")])
    assert turn.usage.prompt_tokens is None
    assert turn.usage.completion_tokens is None
    assert turn.usage.steps == 1


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
    # Distinct args each round, so this is a genuine budget exhaustion (real tool work every
    # step) rather than the repeated-call path that #524's guard would otherwise intercept.
    results = [
        ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"a": 1}')]),
        ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"a": 2}')]),
        ChatResult(model="m", content="final"),
    ]
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"}, outputs={"echo": "x"})
    turn = await Agent(gateway=_FakeGateway(results), mcp=mcp, max_steps=2).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == "max_steps"
    assert turn.content == "final"
    assert mcp.called == [("echo", {"a": 1}), ("echo", {"a": 2})]  # both rounds really ran


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
    def __init__(
        self, text: str = "the attached notes", images: list[ImagePart] | None = None
    ) -> None:
        self._text = text
        self._images = images or []
        self.calls: list[list[Attachment]] = []

    async def expand(self, attachments: list[Attachment], *, tenant: str) -> ExpandedAttachments:
        self.calls.append(attachments)
        return ExpandedAttachments(text=self._text, images=self._images)


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
        async def expand(
            self, attachments: list[Attachment], *, tenant: str
        ) -> ExpandedAttachments:
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


# ── image attachments, gated on model vision support (#633) ─────────────────────────


def _image_part() -> ImagePart:
    return ImagePart(mime="image/png", data_b64="aGVsbG8=", title="photo.png")


async def test_agent_attaches_image_content_when_model_supports_vision() -> None:
    gw = _FakeGateway([ChatResult(model="m", content="I see a cat")], supports_vision=True)
    expander = _FakeExpander(text="", images=[_image_part()])
    msg = ChatMessage(
        role="user",
        content="what is this?",
        attachments=[Attachment(att_id="a1", source="file", title="photo.png")],
    )
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), attachments=expander).run([msg])

    assert turn.content == "I see a cat"
    assert turn.stopped == "completed"
    # the provider call *was* made, with the user message rewritten into content parts
    [sent] = [m for m in gw.calls[0] if m.role == "user"]
    assert sent.content == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]


async def test_agent_blocks_image_before_any_provider_call_when_model_lacks_vision() -> None:
    gw = _FakeGateway(
        [ChatResult(model="m", content="should never be used")], supports_vision=False
    )
    expander = _FakeExpander(text="", images=[_image_part()])
    msg = ChatMessage(
        role="user",
        content="what is this?",
        attachments=[Attachment(att_id="a1", source="file", title="photo.png")],
    )
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), attachments=expander).run([msg])

    assert turn.content == _VISION_UNSUPPORTED_MESSAGE
    assert turn.stopped == _STOPPED_UNSUPPORTED_MEDIA
    assert gw.calls == []  # no provider call at all


async def test_agent_image_turn_is_persisted_with_plain_text_not_the_image_payload() -> None:
    """The user's turn still lands in history — but never the base64 payload (#633)."""
    gw = _FakeGateway([ChatResult(model="m", content="I see a cat")], supports_vision=True)
    expander = _FakeExpander(text="", images=[_image_part()])
    memory = _FakeMemory()
    msg = ChatMessage(
        role="user",
        content="what is this?",
        attachments=[Attachment(att_id="a1", source="file", title="photo.png")],
    )
    await Agent(gateway=gw, mcp=_FakeMcp(), attachments=expander, memory=memory).run(
        [msg], session_id="s1"
    )
    assert memory.remembered == [
        ("user", "what is this?"),
        ("assistant", "I see a cat"),
    ]


async def test_agent_blocked_vision_turn_is_persisted_but_skips_fact_extraction() -> None:
    gw = _FakeGateway([], supports_vision=False)
    expander = _FakeExpander(text="", images=[_image_part()])
    memory = _FakeMemory()

    class _RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[tuple[str, str]] = []

        async def enqueue(self, *, tenant: str, user_text: str, assistant_text: str) -> None:
            self.enqueued.append((user_text, assistant_text))

    queue = _RecordingQueue()
    msg = ChatMessage(
        role="user",
        content="what is this?",
        attachments=[Attachment(att_id="a1", source="file", title="photo.png")],
    )
    await Agent(
        gateway=gw, mcp=_FakeMcp(), attachments=expander, memory=memory, queue=cast(Any, queue)
    ).run([msg], session_id="s1")
    assert memory.remembered == [
        ("user", "what is this?"),
        ("assistant", _VISION_UNSUPPORTED_MESSAGE),
    ]
    await asyncio.sleep(0)  # let the (would-be) fire-and-forget extraction task run
    assert queue.enqueued == []  # a canned rejection is nothing to learn facts from


async def test_agent_without_images_never_calls_supports_vision() -> None:
    """A turn with no image attachments must not pay the vision-capability lookup at all."""

    class _NoVisionCheckGateway(_FakeGateway):
        async def supports_vision(self, *_a: Any, **_k: Any) -> bool:
            raise AssertionError("supports_vision should not be called without an image")

    gw = _NoVisionCheckGateway([ChatResult(model="m", content="ok")])
    turn = await Agent(gateway=gw, mcp=_FakeMcp()).run([ChatMessage(role="user", content="hi")])
    assert turn.content == "ok"


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


async def test_base_prompt_leads_the_memory_path_too() -> None:
    """Injection-first on the *memory* path — not just the headless early return (#536).

    With a session, ``_assemble`` builds recall + history, so the assembled order is
    ``[system(instructions), system(recalled), ...history..., new user]``: the base prompt is
    still message[0], ahead of the recalled-memory system block.
    """
    gw = _FakeGateway([ChatResult(model="m", content="answer")])
    store = await _fresh_instructions(default="You are epsilon.")
    memory = _FakeMemory(
        recalled=["the user's name is Sam"],
        history=[
            ChatMessage(role="user", content="earlier question"),
            ChatMessage(role="assistant", content="earlier answer"),
        ],
    )
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=memory, instructions=store)
    await agent.run([ChatMessage(role="user", content="what's my name?")], session_id="s1")

    sent = gw.calls[0]
    # The base prompt leads; the recalled-memory system block comes *after* it, not before.
    assert sent[0].role == "system" and sent[0].content == "You are epsilon."
    assert sent[1].role == "system" and "Sam" in (sent[1].content or "")
    # Then the prior history, then the new user turn — order preserved.
    assert [m.content for m in sent if m.role == "user"] == ["earlier question", "what's my name?"]
    assert any(m.role == "assistant" and m.content == "earlier answer" for m in sent)


# ── Loop hygiene: repeated calls + error streaks (#524) ───────────────────────


def test_canonical_calls_is_order_free_but_arg_sensitive() -> None:
    a = _tool_call("echo", '{"x": 1, "y": 2}', "c1")
    b = _tool_call("read", '{"p": 5}', "c2")
    # The same set of calls in either order canonicalizes identically…
    assert _canonical_calls([a, b]) == _canonical_calls([b, a])
    # …and key order inside the arguments doesn't matter either.
    assert _canonical_calls([_tool_call("echo", '{"y": 2, "x": 1}')]) == _canonical_calls([a])
    # …but different arguments never collide (paging / per-item calls stay distinct).
    assert _canonical_calls([_tool_call("echo", '{"x": 2}')]) != _canonical_calls(
        [_tool_call("echo", '{"x": 1}')]
    )


def test_loop_guard_repeat_verdict_new_then_nudge_then_stop() -> None:
    guard = _LoopGuard()
    same = [_tool_call("echo", "{}")]
    assert guard.repeat_verdict(same) == "new"  # first sight
    assert guard.repeat_verdict(same) == "nudge"  # immediate repeat → one-shot nudge
    assert guard.repeat_verdict(same) == "stop"  # a further repeat → stop
    # The nudge is one-shot per turn: a fresh distinct call is "new", but its own repeat now stops
    # straight away (the single nudge is already spent) — matching _ANSWER_NUDGE's one-shot rule.
    assert guard.repeat_verdict([_tool_call("echo", '{"x": 1}')]) == "new"
    assert guard.repeat_verdict([_tool_call("echo", '{"x": 1}')]) == "stop"


def test_loop_guard_error_streak_counts_consecutive_and_resets() -> None:
    assert _MAX_CONSECUTIVE_TOOL_ERRORS == 3
    guard = _LoopGuard()
    assert guard.note_results([True]) is False  # streak 1
    assert guard.note_results([True]) is False  # streak 2
    assert guard.note_results([False]) is False  # a success resets the streak to 0
    assert guard.note_results([True, True]) is False  # 1, 2 within one step
    assert guard.note_results([True]) is True  # 3 consecutive → stop


@pytest.mark.timeout(10)
async def test_repeated_identical_call_nudges_then_stops() -> None:
    # The model re-issues the exact same call three times: the first repeat earns a one-shot
    # nudge, the second stops the turn (repeat_call). The tool runs ONCE — not re-executed each
    # time (a repeated write would double-apply) — and a real final answer replaces the silent stop.
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", "{}")]),
            ChatResult(model="m", content="here is the answer"),
        ]
    )
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"})
    turn = await Agent(gateway=gw, mcp=mcp, max_steps=6).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == _STOPPED_REPEAT_CALL
    assert turn.content == "here is the answer"
    assert mcp.called == [("echo", {})]  # invoked once, not three times
    # the one-shot nudge was injected after the first repeat (step 3's convo carries it)
    assert any(m.role == "user" and m.content == _REPEAT_NUDGE for m in gw.calls[2])


@pytest.mark.timeout(10)
async def test_distinct_args_repeats_are_not_flagged() -> None:
    # Paging / per-item calls: the same tool with *different* arguments must pass untouched.
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"page": 1}')]),
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"page": 2}')]),
            ChatResult(model="m", content="all pages read"),
        ]
    )
    mcp = _FakeMcp(specs=[_echo_spec()], route={"echo": "u"})
    turn = await Agent(gateway=gw, mcp=mcp, max_steps=6).run(
        [ChatMessage(role="user", content="read all")]
    )
    assert turn.stopped == "completed"
    assert turn.content == "all pages read"
    assert mcp.called == [("echo", {"page": 1}), ("echo", {"page": 2})]  # both ran
    assert not any(m.role == "user" and m.content == _REPEAT_NUDGE for c in gw.calls for m in c)


class _AlwaysFailMcp(_FakeMcp):
    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
        self.called.append((name, arguments))
        raise ToolCallError("boom: cannot do that")


@pytest.mark.timeout(10)
async def test_error_streak_stops_early_with_what_failed() -> None:
    # The model retries variants of a broken call (distinct args, so this is the error-streak
    # path, not the repeat path). Three consecutive errors stop the turn early (tool_errors)
    # rather than burning every step, and it answers with what failed.
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"try": 1}')]),
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"try": 2}')]),
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"try": 3}')]),
            ChatResult(model="m", content="that didn't work; here's what failed"),
        ]
    )
    mcp = _AlwaysFailMcp(specs=[_echo_spec()], route={"echo": "u"})
    turn = await Agent(gateway=gw, mcp=mcp, max_steps=10).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == _STOPPED_TOOL_ERRORS
    assert turn.content == "that didn't work; here's what failed"
    assert len(mcp.called) == 3  # stopped after the 3rd error, well before max_steps=10


class _PickyMcp(_FakeMcp):
    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
        self.called.append((name, arguments))
        if arguments.get("ok"):
            return "worked"
        raise ToolCallError("boom")


@pytest.mark.timeout(10)
async def test_error_streak_resets_on_a_successful_call() -> None:
    # A success between errors resets the streak: a turn that errors, recovers, then errors again
    # (never three in a row) is NOT cut short — no over-eager stopping of an otherwise healthy turn.
    gw = _FakeGateway(
        [
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"n": 1}')]),  # err
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"n": 2}')]),  # err
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"ok": 1}')]),  # ok
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"n": 3}')]),  # err
            ChatResult(model="m", content="", tool_calls=[_tool_call("echo", '{"n": 4}')]),  # err
            ChatResult(model="m", content="done despite the bumps"),
        ]
    )
    mcp = _PickyMcp(specs=[_echo_spec()], route={"echo": "u"})
    turn = await Agent(gateway=gw, mcp=mcp, max_steps=10).run(
        [ChatMessage(role="user", content="go")]
    )
    assert turn.stopped == "completed"
    assert turn.content == "done despite the bumps"
    assert len(mcp.called) == 5  # all ran; the success reset the streak so 3-in-a-row never hit


# ── Standing profile: static injection, no turn-time embed (#527) ─────────────


class _FakeProfile:
    """A standing-profile store stand-in — returns a fixed profile (or None / an error)."""

    def __init__(self, content: str | None = None, *, fail: bool = False) -> None:
        self._content = content
        self._fail = fail

    async def latest(self, *, tenant: str) -> StandingProfile | None:
        if self._fail:
            raise RuntimeError("profile db down")
        if not self._content:
            return None
        return StandingProfile(id=1, content=self._content, source="auto")


async def test_profile_injected_as_a_static_leading_block() -> None:
    # The profile is injected as a system block with NO embed — even headless (no session),
    # where recall never runs. This is the whole point: the common case leaves the response path.
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    profile = _FakeProfile("The user lives in Belgrade and prefers metric units.")
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), profile=profile).run(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="hello")]
    )
    assert turn.content == "hi"
    sent = gw.calls[0]
    assert sent[0].role == "system" and "Belgrade" in (sent[0].content or "")
    assert [m.role for m in sent] == ["system", "user"]  # profile block, then the user message


async def test_no_profile_is_todays_behavior() -> None:
    # No stored profile leaves the assembled turn exactly as before — no system block injected.
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), profile=_FakeProfile(None)).run(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="hello")]
    )
    assert turn.content == "hi"
    assert [m.role for m in gw.calls[0]] == ["user"]


async def test_profile_precedes_recalled_facts() -> None:
    # With memory + a session, the static profile leads and the (embedded) recalled facts follow:
    # both coexist — the profile covers the common case, recall stays for the long-tail specifics.
    gw = _FakeGateway([ChatResult(model="m", content="answer")])
    memory = _FakeMemory(recalled=["the user is planning a trip to Rome"])
    profile = _FakeProfile("The user lives in Belgrade.")
    agent = Agent(gateway=gw, mcp=_FakeMcp(), memory=memory, profile=profile)  # type: ignore[arg-type]
    await agent.run([ChatMessage(role="user", content="where next?")], session_id="s1")
    systems = [m.content or "" for m in gw.calls[0] if m.role == "system"]
    assert "Belgrade" in systems[0]  # profile block first
    assert "Rome" in systems[1]  # recalled-facts block second


async def test_profile_read_failure_degrades_to_no_profile() -> None:
    # A profile read error must never break the turn — proceed without it.
    gw = _FakeGateway([ChatResult(model="m", content="hi")])
    profile = _FakeProfile("x", fail=True)
    turn = await Agent(gateway=gw, mcp=_FakeMcp(), profile=profile).run(  # type: ignore[arg-type]
        [ChatMessage(role="user", content="hello")]
    )
    assert turn.content == "hi"
    assert [m.role for m in gw.calls[0]] == ["user"]  # degraded to no profile
