"""Unit tests for the nightly playbook-reflection pass (ADR-0093 §1/§5/§6).

The gateway is faked with recorded shapes — no live keys, no real model. What matters here is the
orchestration: what gets scanned, what gets staged, what is metered under which tenant, and the
hard rule that this pass never applies anything itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import ChatMessage, ChatResult
from epicurus_core_app.agent.instructions import AgentInstructionsStore
from epicurus_core_app.agent.playbook_review import (
    INSTRUCTIONS_PATH,
    CoreReviewPage,
    PlaybookProposalStore,
    playbook_path,
)
from epicurus_core_app.agent.playbooks import PlaybookStore
from epicurus_core_app.agent.reflection import (
    PlaybookReflector,
    ReflectionStateStore,
)
from epicurus_core_app.llm.power import GatewayPausedError

TENANT = "t1"


class _FakeChat:
    """Records every call and replies with canned content (a recorded gateway shape)."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, object]] | None = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        self.calls.append({"messages": messages, "model": model, "tenant_id": tenant_id})
        reply = self._replies.pop(0) if self._replies else '{"proposals": []}'
        return ChatResult(model="m", content=reply)

    @property
    def prompts(self) -> list[str]:
        """The user-turn text of each call."""
        return [str(c["messages"][-1].content) for c in self.calls]


class _PausedChat(_FakeChat):
    async def chat(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise GatewayPausedError("the gateway is paused")


class _Session:
    def __init__(self, sid: str, title: str, last_at: datetime) -> None:
        self.id = sid
        self.title = title
        self.last_at = last_at


class _FakeSessions:
    """An in-memory stand-in for the conversation store's read surface."""

    def __init__(self) -> None:
        self.data: dict[str, list[tuple[_Session, list[tuple[str, str]]]]] = {}

    def add(
        self,
        tenant: str,
        sid: str,
        title: str,
        last_at: datetime,
        messages: list[tuple[str, str]],
    ) -> None:
        self.data.setdefault(tenant, []).append((_Session(sid, title, last_at), messages))

    async def distinct_tenants(self) -> list[str]:
        return list(self.data)

    async def sessions(self, *, tenant: str) -> list[_Session]:
        # The real store returns most-recently-active first; mirror that here.
        rows = self.data.get(tenant, [])
        return [s for s, _ in sorted(rows, key=lambda r: r[0].last_at, reverse=True)]

    async def history(self, *, tenant: str, session_id: str) -> list[tuple[str, str]]:
        for s, msgs in self.data.get(tenant, []):
            if s.id == session_id:
                return msgs
        return []


def _engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


class _Harness:
    def __init__(
        self,
        reflector: PlaybookReflector,
        chat: _FakeChat,
        sessions: _FakeSessions,
        proposals: PlaybookProposalStore,
        playbooks: PlaybookStore,
        state: ReflectionStateStore,
        page: CoreReviewPage,
        instructions: AgentInstructionsStore,
    ) -> None:
        self.reflector = reflector
        self.chat = chat
        self.sessions = sessions
        self.proposals = proposals
        self.playbooks = playbooks
        self.state = state
        self.page = page
        self.instructions = instructions


def _reply(*proposals: dict[str, Any]) -> str:
    return json.dumps({"proposals": list(proposals)})


async def _fresh(
    replies: list[str] | None = None, chat: _FakeChat | None = None, model: str | None = None
) -> _Harness:
    engine = _engine()
    playbooks = PlaybookStore(engine)
    proposals = PlaybookProposalStore(engine)
    state = ReflectionStateStore(engine)
    instructions = AgentInstructionsStore(engine, default="BASE", playbooks=playbooks)
    for s in (playbooks, proposals, state, instructions):
        await s.init()
    sessions = _FakeSessions()
    the_chat = chat or _FakeChat(replies)
    reflector = PlaybookReflector(
        the_chat, sessions, proposals, playbooks, instructions, state, model=model
    )
    page = CoreReviewPage(
        store=proposals,
        instructions=instructions,
        playbooks=playbooks,
        tenant=TENANT,
        version="9.9.9",
    )
    return _Harness(reflector, the_chat, sessions, proposals, playbooks, state, page, instructions)


def _seed(h: _Harness, tenant: str = TENANT, sid: str = "s1") -> None:
    h.sessions.add(
        tenant,
        sid,
        "Morning routine",
        datetime.now(UTC),
        [("user", "check calendar first, then mail"), ("assistant", "will do")],
    )


# ── metering (ADR-0093 §5) — the brief's explicit requirement ────────────────


async def test_the_gateway_call_is_metered_under_the_scanned_tenant() -> None:
    """Never the default tenant: an off-hours job's usage belongs to whoever owns the data."""
    h = await _fresh()
    _seed(h, tenant="acme")

    await h.reflector.run()
    assert [c["tenant_id"] for c in h.chat.calls] == ["acme"]


async def test_each_tenant_is_metered_under_itself_in_a_fan_out() -> None:
    h = await _fresh()
    _seed(h, tenant="acme", sid="a1")
    _seed(h, tenant="globex", sid="g1")

    await h.reflector.run()
    assert sorted(c["tenant_id"] for c in h.chat.calls) == ["acme", "globex"]
    # And never a synthetic background/default tenant.
    assert "local" not in {c["tenant_id"] for c in h.chat.calls}


async def test_exactly_one_gateway_call_per_tenant_per_pass() -> None:
    """ADR-0093 §1: *one* LLM call over the tenant's activity, not one per session."""
    h = await _fresh()
    _seed(h, sid="s1")
    h.sessions.add(TENANT, "s2", "Other", datetime.now(UTC), [("user", "hi")])
    h.sessions.add(TENANT, "s3", "More", datetime.now(UTC), [("user", "hello")])

    await h.reflector.run()
    assert len(h.chat.calls) == 1


# ── the hard constraint: propose, never apply ────────────────────────────────


async def test_a_proposal_is_staged_for_review_and_never_applied() -> None:
    h = await _fresh(
        [_reply({"target": "instructions", "content": "Be terse.", "note": "asked twice"})]
    )
    _seed(h)

    assert await h.reflector.run() == 1
    # Staged, awaiting the operator...
    [s] = (await h.page.list_review()).suggestions
    assert s.path == INSTRUCTIONS_PATH
    assert s.content == "Be terse."
    assert s.origin == "reflection"
    assert s.note == "asked twice"
    # ...and the live document is untouched until they approve.
    assert await h.instructions.get_base(TENANT) == "BASE"


async def test_a_playbook_proposal_never_creates_the_playbook() -> None:
    h = await _fresh(
        [_reply({"target": "playbook", "name": "Briefing", "content": "Calendar first."})]
    )
    _seed(h)

    assert await h.reflector.run() == 1
    assert await h.playbooks.list_playbooks(TENANT) == []  # nothing written
    [s] = (await h.page.list_review()).suggestions
    assert s.path == playbook_path("Briefing")


# ── operation is derived, not trusted ────────────────────────────────────────


async def test_a_new_playbook_is_staged_as_a_create() -> None:
    h = await _fresh([_reply({"target": "playbook", "name": "Briefing", "content": "x"})])
    _seed(h)
    await h.reflector.run()

    [s] = (await h.page.list_review()).suggestions
    assert s.operation == "create"
    assert s.current == ""


async def test_an_existing_playbook_is_staged_as_an_update_even_if_the_model_says_create() -> None:
    """The operation is derived from what exists — a mislabelled create would render an empty
    `current` side and hide what the approval is about to overwrite."""
    h = await _fresh(
        [
            _reply(
                {
                    "target": "playbook",
                    "name": "Briefing",
                    "content": "new text",
                    "operation": "create",
                }
            )
        ]
    )
    await h.playbooks.create(TENANT, name="Briefing", content="old text")
    _seed(h)
    await h.reflector.run()

    [s] = (await h.page.list_review()).suggestions
    assert s.operation == "update"
    assert s.current == "old text"  # the operator sees exactly what they'd replace


async def test_base_instructions_are_always_an_update() -> None:
    h = await _fresh([_reply({"target": "instructions", "content": "x", "operation": "create"})])
    _seed(h)
    await h.reflector.run()

    [s] = (await h.page.list_review()).suggestions
    assert s.operation == "update"


# ── the model sees what it's editing (#658) ──────────────────────────────────


async def test_the_prompt_shows_the_current_base_instructions() -> None:
    h = await _fresh()
    await h.instructions.set_instructions(TENANT, "Be concise and cite sources.")
    _seed(h)

    await h.reflector.run()
    assert "Be concise and cite sources." in h.chat.prompts[0]


async def test_the_prompt_shows_the_shipped_default_when_no_base_is_set() -> None:
    h = await _fresh()
    _seed(h)

    await h.reflector.run()
    assert "BASE" in h.chat.prompts[0]  # the harness's AgentInstructionsStore default


async def test_the_prompt_shows_an_existing_playbooks_current_content() -> None:
    h = await _fresh()
    await h.playbooks.create(TENANT, name="Briefing", content="Check calendar before mail.")
    _seed(h)

    await h.reflector.run()
    prompt = h.chat.prompts[0]
    assert "Briefing" in prompt
    assert "Check calendar before mail." in prompt


async def test_a_disabled_playbooks_content_is_still_shown() -> None:
    """`_stage`'s dedup already treats a disabled playbook as known (a proposal against it is an
    update, not a duplicate create) — the prompt must show the same set, or the model could be
    asked to "update" a document it was never actually shown."""
    h = await _fresh()
    p = await h.playbooks.create(TENANT, name="Old flow", content="stale guidance")
    await h.playbooks.set_enabled(TENANT, p.id, False)
    _seed(h)

    await h.reflector.run()
    assert "stale guidance" in h.chat.prompts[0]


async def test_the_prompt_only_shows_the_scanned_tenants_own_documents() -> None:
    """A cross-tenant leak would surface here: two tenants, each with distinct base instructions
    and a distinct playbook, both scanned in the same `run()` fan-out (constraint #1)."""
    h = await _fresh()
    await h.instructions.set_instructions(TENANT, "acme's own prompt")
    await h.playbooks.create(TENANT, name="Acme playbook", content="acme's own guidance")
    await h.instructions.set_instructions("globex", "globex's own prompt")
    await h.playbooks.create("globex", name="Globex playbook", content="globex's own guidance")
    _seed(h, tenant=TENANT, sid="a1")
    _seed(h, tenant="globex", sid="g1")

    await h.reflector.run()
    calls = {c["tenant_id"]: str(c["messages"][-1].content) for c in h.chat.calls}
    assert "acme's own prompt" in calls[TENANT]
    assert "globex's own prompt" not in calls[TENANT]
    assert "globex's own prompt" in calls["globex"]
    assert "acme's own prompt" not in calls["globex"]


# ── the scan window + watermark ──────────────────────────────────────────────


async def test_only_sessions_active_since_the_last_run_are_scanned() -> None:
    h = await _fresh()
    old = datetime.now(UTC) - timedelta(days=5)
    h.sessions.add(TENANT, "old", "Ancient", old, [("user", "stale")])
    await h.state.mark_run(TENANT, datetime.now(UTC) - timedelta(days=1))

    assert await h.reflector.run() == 0
    assert h.chat.calls == []  # nothing new → no gateway call at all


async def test_a_session_newer_than_the_watermark_is_scanned() -> None:
    h = await _fresh()
    await h.state.mark_run(TENANT, datetime.now(UTC) - timedelta(days=1))
    h.sessions.add(TENANT, "new", "Fresh", datetime.now(UTC), [("user", "today's chat")])

    await h.reflector.run()
    assert len(h.chat.calls) == 1
    assert "today's chat" in h.chat.prompts[0]


async def test_the_first_ever_pass_scans_everything() -> None:
    h = await _fresh()
    h.sessions.add(
        TENANT, "s", "T", datetime.now(UTC) - timedelta(days=30), [("user", "ancient but unseen")]
    )

    await h.reflector.run()
    assert "ancient but unseen" in h.chat.prompts[0]


async def test_the_watermark_advances_after_a_pass() -> None:
    h = await _fresh()
    _seed(h)
    assert await h.state.last_run(TENANT) is None

    await h.reflector.run()
    assert await h.state.last_run(TENANT) is not None
    # A second pass has nothing new to read.
    await h.reflector.run()
    assert len(h.chat.calls) == 1


async def test_the_watermark_advances_even_when_nothing_was_active() -> None:
    """No work to come back to — don't leave the window open and re-scan forever."""
    h = await _fresh()
    h.sessions.data[TENANT] = []
    await h.reflector.run()
    assert await h.state.last_run(TENANT) is not None


async def test_the_watermark_is_tenant_scoped() -> None:
    h = await _fresh()
    _seed(h, tenant="acme", sid="a1")
    _seed(h, tenant="globex", sid="g1")
    await h.reflector.run()

    assert await h.state.last_run("acme") is not None
    assert await h.state.last_run("globex") is not None
    assert await h.state.last_run("nobody") is None


async def test_a_failing_tenant_does_not_wedge_the_batch() -> None:
    class _OneBadTenant(_FakeChat):
        async def chat(self, messages: Any, **kwargs: Any) -> ChatResult:
            if kwargs.get("tenant_id") == "acme":
                raise RuntimeError("model exploded")
            return await super().chat(messages, **kwargs)

    h = await _fresh(chat=_OneBadTenant([_reply({"target": "instructions", "content": "ok"})]))
    _seed(h, tenant="acme", sid="a1")
    _seed(h, tenant="globex", sid="g1")

    await h.reflector.run()  # must not raise
    assert await h.state.last_run("acme") is None  # not marked — it never completed
    assert await h.state.last_run("globex") is not None


async def test_a_paused_gateway_stops_the_batch() -> None:
    h = await _fresh(chat=_PausedChat())
    _seed(h, tenant="acme", sid="a1")
    _seed(h, tenant="globex", sid="g1")

    assert await h.reflector.run() == 0
    assert await h.state.last_run("acme") is None


# ── rejection feedback (ADR-0093 §6) ─────────────────────────────────────────


async def test_recently_rejected_proposals_are_given_as_negative_context() -> None:
    h = await _fresh()
    p = await h.proposals.add(
        tenant=TENANT,
        path=playbook_path("Bad idea"),
        operation="create",
        proposed_content="always cc the whole team",
    )
    await h.page.reject(p.sid)
    _seed(h)

    await h.reflector.run()
    prompt = h.chat.prompts[0]
    assert "REJECTED" in prompt
    assert "always cc the whole team" in prompt


async def test_approved_proposals_are_not_given_as_negative_context() -> None:
    """Only rejections are negative — an approval is not something to avoid re-proposing."""
    h = await _fresh()
    p = await h.proposals.add(
        tenant=TENANT, path=INSTRUCTIONS_PATH, operation="update", proposed_content="a good idea"
    )
    await h.page.approve(p.sid)
    _seed(h)

    await h.reflector.run()
    # Approving writes "a good idea" as the tenant's actual base instructions, so it correctly
    # reappears in the *current documents* section (#658) — what must NOT happen is the separate
    # negative-context block, which only renders when something was actually rejected.
    assert "REJECTED" not in h.chat.prompts[0]


async def test_another_tenants_rejections_are_never_shown() -> None:
    h = await _fresh()
    p = await h.proposals.add(
        tenant="other", path=INSTRUCTIONS_PATH, operation="update", proposed_content="their secret"
    )
    await h.proposals.record(tenant="other", proposal=p, decision="rejected")
    _seed(h)

    await h.reflector.run()
    assert "their secret" not in h.chat.prompts[0]


# ── duplicate suppression + junk tolerance ───────────────────────────────────


async def test_a_document_with_a_pending_proposal_is_not_proposed_again() -> None:
    """Don't stack drafts while the operator is away."""
    h = await _fresh([_reply({"target": "instructions", "content": "second thought"})])
    await h.proposals.add(
        tenant=TENANT, path=INSTRUCTIONS_PATH, operation="update", proposed_content="first thought"
    )
    _seed(h)

    assert await h.reflector.run() == 0
    [s] = (await h.page.list_review()).suggestions
    assert s.content == "first thought"  # the original still stands


async def test_a_reply_naming_one_document_twice_stages_it_once() -> None:
    h = await _fresh(
        [
            _reply(
                {"target": "instructions", "content": "one"},
                {"target": "instructions", "content": "two"},
            )
        ]
    )
    _seed(h)
    assert await h.reflector.run() == 1


async def test_an_empty_proposal_list_stages_nothing() -> None:
    h = await _fresh(['{"proposals": []}'])
    _seed(h)
    assert await h.reflector.run() == 0
    assert (await h.page.list_review()).suggestions == []


async def test_a_non_json_reply_stages_nothing_rather_than_raising() -> None:
    """A bad generation must cost the operator nothing."""
    h = await _fresh(["I'm afraid I can't do that."])
    _seed(h)
    assert await h.reflector.run() == 0


async def test_a_fenced_json_reply_is_still_parsed() -> None:
    h = await _fresh(['```json\n{"proposals": [{"target": "instructions", "content": "x"}]}\n```'])
    _seed(h)
    assert await h.reflector.run() == 1


async def test_a_proposal_with_no_content_is_dropped() -> None:
    h = await _fresh([_reply({"target": "instructions", "content": "   "})])
    _seed(h)
    assert await h.reflector.run() == 0


async def test_a_playbook_proposal_with_no_name_is_dropped() -> None:
    h = await _fresh([_reply({"target": "playbook", "content": "orphan guidance"})])
    _seed(h)
    assert await h.reflector.run() == 0


async def test_an_unknown_target_is_dropped() -> None:
    h = await _fresh([_reply({"target": "the_database", "content": "DROP TABLE"})])
    _seed(h)
    assert await h.reflector.run() == 0


async def test_an_absurdly_long_proposal_is_dropped() -> None:
    h = await _fresh([_reply({"target": "instructions", "content": "x" * 9_000})])
    _seed(h)
    assert await h.reflector.run() == 0


async def test_a_valid_proposal_survives_alongside_an_invalid_one() -> None:
    h = await _fresh(
        [
            _reply(
                {"target": "nonsense", "content": "junk"},
                {"target": "playbook", "name": "Good", "content": "keep me"},
            )
        ]
    )
    _seed(h)
    assert await h.reflector.run() == 1
    [s] = (await h.page.list_review()).suggestions
    assert s.title == "Good"


# ── the model override ───────────────────────────────────────────────────────


async def test_the_configured_model_is_passed_to_the_gateway() -> None:
    """Constructed with `model=`, the same kwarg `settings.playbook_reflection_model` reaches
    in `app.py` — not poked onto the instance after the fact, which would leave the constructor
    wiring itself uncovered (#658)."""
    h = await _fresh(model="small-model")
    _seed(h)
    await h.reflector.run()
    assert h.chat.calls[0]["model"] == "small-model"
