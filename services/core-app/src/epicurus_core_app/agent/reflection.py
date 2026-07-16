"""Nightly reflection — the agent proposes edits to its own guidance, never applies them.

Once a night (on the maintenance orchestrator's batch, ADR-0060) this scans each tenant's
sessions active since the last reflection run and makes **one** gateway call asking for candidate
edits to the base instructions or a named playbook: recurring corrections, discovered procedures.
Each candidate is **staged** as a ``ReviewSuggestion`` on the core's own review page
(``playbook_review.py``); the operator approves or rejects. This module writes proposals and
nothing else (ADR-0093 §1 + its hard non-goal).

**That constraint is enforced structurally, not by discipline**: the reflector is handed a
proposal *sink* and a read-only playbook *lookup* (the Protocols below), never the stores that
own the documents. There is no code path from here to ``agent_instructions`` /
``agent_playbooks`` — the only way in is the operator's Approve.

**Rejection feedback (ADR-0093 §6).** Recently rejected proposals are digested into the prompt as
explicit negative context, read from the same audit trail #542 shipped, so the pass doesn't
re-propose what the operator already declined. This is the concrete payoff of building that trail
first: playbooks needed a "what did the operator already say no to" memory, and one existed.

**Metering (ADR-0093 §5).** The gateway call threads ``tenant_id=<the tenant whose sessions it
scanned>`` — an off-hours job's usage is attributed to the tenant that owns the underlying data,
never a synthetic background tenant (the ADR-0051 extraction-drain precedent; constraints #1/#8).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import DateTime, String
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import ChatMessage, ChatResult, get_logger
from epicurus_core.review import ReviewDecision
from epicurus_core_app.agent.playbook_review import (
    INSTRUCTIONS_PATH,
    PlaybookProposal,
    playbook_path,
)
from epicurus_core_app.agent.playbooks import AgentPlaybook
from epicurus_core_app.llm.power import GatewayPausedError

log = get_logger("epicurus_core_app.agent.reflection")

# How many of the tenant's recently-active sessions the pass reads, and how many messages of each.
# The prompt is one call over a night's activity, not an archive crawl — a bound keeps the pass
# cheap and inside the window regardless of how busy the day was.
MAX_SESSIONS = 20
MAX_MESSAGES_PER_SESSION = 40

# How many recently rejected proposals are digested as negative context (ADR-0093 §6). Enough to
# cover a pattern the operator keeps declining, small enough not to crowd out the transcripts.
MAX_REJECTIONS = 10

# A proposal's content is guidance, not an essay; a runaway generation is a symptom, not a
# proposal worth staging. Well above any plausible playbook, well below prompt-crowding size.
MAX_PROPOSAL_CHARS = 8_000

_SYSTEM_PROMPT = """\
You review an AI assistant's recent conversations with its operator and propose improvements to \
the assistant's own standing guidance. You are looking for durable lessons, not one-off details: \
a correction the operator had to repeat, a procedure that worked and should be reused, a \
preference stated as a general rule.

You may propose changes to two kinds of document:
- "instructions": the assistant's base system prompt — its identity, voice, and general rules.
- "playbook": a named block of guidance for a recurring task (e.g. "Morning briefing").

Rules:
- Propose ONLY what the transcripts actually support. If nothing durable emerged, propose nothing.
- Prefer a playbook for anything task-specific; reserve the base instructions for general rules.
- Give the FULL new text of the document, not a diff or a fragment — it replaces what is there.
- Never propose a change that was already rejected below, unless the transcripts show the \
pattern has meaningfully changed.
- Do not invent facts, preferences, or events the transcripts do not show.

Reply with JSON only, in this exact shape:
{"proposals": [{"target": "instructions" | "playbook", "name": "<playbook name, omit for \
instructions>", "content": "<the full new text>", "note": "<one sentence: why, citing what you \
saw>"}]}

If you have nothing worth proposing, reply exactly: {"proposals": []}"""


class _ReflectionBase(DeclarativeBase):
    pass


class _ReflectionStateRow(_ReflectionBase):
    """The per-tenant watermark: when this tenant was last reflected on.

    Durable rather than in-memory (constraint #2 — services are stateless, state is externalized):
    an in-process marker would reset on every restart and re-scan the whole history, re-proposing
    lessons the operator has already seen.
    """

    __tablename__ = "agent_reflection_state"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    last_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReflectionStateStore:
    """Reads/writes the per-tenant last-reflection watermark."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_ReflectionBase.metadata.create_all)

    async def last_run(self, tenant: str) -> datetime | None:
        """When *tenant* was last reflected on, or ``None`` if never (scan everything once).

        Always **aware** UTC, normalized here at the boundary: Postgres hands back an aware
        datetime but SQLite hands back a naive one even from a ``timezone=True`` column, and
        comparing the two raises. Normalizing once here means no caller has to know which
        backend it is talking to.
        """
        async with self._session() as session:
            row = await session.get(_ReflectionStateRow, tenant)
            return _as_utc(row.last_run_at) if row is not None else None

    async def mark_run(self, tenant: str, when: datetime) -> None:
        """Record that *tenant* has now been reflected on up to *when*."""
        async with self._session() as session:
            row = await session.get(_ReflectionStateRow, tenant)
            if row is None:
                session.add(_ReflectionStateRow(tenant=tenant, last_run_at=when))
            else:
                row.last_run_at = when
            await session.commit()


class _ChatModel(Protocol):
    """The slice of the LLM gateway this pass needs (eases faking in tests)."""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = ...,
        tools: list[dict[str, object]] | None = ...,
        tenant_id: str | None = ...,
    ) -> ChatResult: ...


class _SessionSummaryLike(Protocol):
    id: str
    title: str
    last_at: datetime


class _SessionSource(Protocol):
    """The slice of the conversation store this pass reads — history, never writes.

    The reads return ``Sequence``, not ``list``: ``list`` is invariant, so a store returning its
    own concrete ``list[SessionSummary]`` would not satisfy a ``list[_SessionSummaryLike]``
    protocol. ``Sequence`` is covariant and read-only — which is exactly what this pass wants.
    """

    async def distinct_tenants(self) -> list[str]: ...

    async def sessions(self, *, tenant: str) -> Sequence[_SessionSummaryLike]: ...

    async def history(self, *, tenant: str, session_id: str) -> Sequence[tuple[str, str]]: ...


class _PlaybookLookup(Protocol):
    """The **read-only** slice of the playbook store. Deliberately no write method exists here:
    this pass may learn what playbooks are called, never change one (ADR-0093's hard non-goal)."""

    async def list_playbooks(
        self, tenant: str, *, enabled_only: bool = ...
    ) -> Sequence[AgentPlaybook]: ...


class _ProposalSink(Protocol):
    """The staging surface: add a proposal, and read what was already staged or declined.

    The only write this module can reach. Approving a staged proposal is the operator's act, on
    the review page — there is no path to it from here.
    """

    async def add(
        self,
        *,
        tenant: str,
        path: str,
        operation: str,
        proposed_content: str,
        origin: str = ...,
        note: str = ...,
    ) -> PlaybookProposal: ...

    async def list_pending(self, *, tenant: str) -> list[PlaybookProposal]: ...

    async def decisions(
        self, *, tenant: str, limit: int = ..., decision: str | None = ...
    ) -> list[ReviewDecision]: ...


class PlaybookReflector:
    """Proposes candidate edits to the agent's own guidance from what it saw in use.

    ``tenants`` (defaulting to the conversation store's own fan-out) yields the tenants to reflect
    on, so the pass is tenant-first (constraint #1) even though v1 has one.
    """

    def __init__(
        self,
        chat: _ChatModel,
        sessions: _SessionSource,
        proposals: _ProposalSink,
        playbooks: _PlaybookLookup,
        state: ReflectionStateStore,
        *,
        tenants: Callable[[], Awaitable[list[str]]] | None = None,
        model: str | None = None,
        max_sessions: int = MAX_SESSIONS,
    ) -> None:
        self._chat = chat
        self._sessions = sessions
        self._proposals = proposals
        self._playbooks = playbooks
        self._state = state
        self._tenants = tenants or sessions.distinct_tenants
        self._model = model
        self._max_sessions = max_sessions

    async def run(self) -> int:
        """Reflect on every tenant once; returns how many proposals were staged.

        The nightly-job entry point. Best-effort per tenant — one tenant's failure is logged and
        skipped, never aborting the rest — but a paused gateway stops the batch (the model is
        asleep; leave the remainder for the next window), mirroring the extraction drain and the
        profile synthesizer.
        """
        staged = 0
        for tenant in await self._tenants():
            try:
                staged += await self.reflect(tenant=tenant)
            except GatewayPausedError:
                log.info("playbook reflection stopped; gateway paused", staged=staged)
                break
            except Exception as exc:  # one tenant's failure must not wedge the batch
                log.warning(
                    "playbook reflection failed for a tenant", tenant=tenant, error=str(exc)
                )
        if staged:
            log.info("nightly playbook reflection complete", proposals=staged)
        return staged

    async def reflect(self, *, tenant: str) -> int:
        """Reflect on one tenant's recent sessions; returns how many proposals were staged.

        The watermark advances **only** on a completed pass, and is snapshotted *before* the scan
        so a session written mid-pass is re-read next time rather than skipped. Re-reading is
        harmless (a duplicate proposal is suppressed below); losing a session is not.
        """
        started = datetime.now(UTC)
        since = await self._state.last_run(tenant)
        transcripts = await self._recent_transcripts(tenant=tenant, since=since)
        if not transcripts:
            # Nothing happened since the last pass — don't spend a gateway call to be told so.
            # The watermark still advances: there is no work here to come back to.
            await self._state.mark_run(tenant, started)
            return 0

        rejected = await self._proposals.decisions(
            tenant=tenant, limit=MAX_REJECTIONS, decision="rejected"
        )
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=self._build_prompt(transcripts, rejected)),
        ]
        # The scanned tenant, never the default — an off-hours job's usage belongs to whoever owns
        # the data it read (ADR-0093 §5 / the ADR-0051 precedent). Constraints #1 and #8.
        result = await self._chat.chat(messages, model=self._model, tenant_id=tenant)
        staged = await self._stage(tenant=tenant, content=result.content or "")
        await self._state.mark_run(tenant, started)
        return staged

    async def _recent_transcripts(
        self, *, tenant: str, since: datetime | None
    ) -> list[tuple[str, list[tuple[str, str]]]]:
        """The tenant's sessions active since *since*, newest first, as ``(title, messages)``.

        ``sessions`` is already ordered most-recently-active first, so the bound keeps the
        freshest activity rather than an arbitrary slice.
        """
        out: list[tuple[str, list[tuple[str, str]]]] = []
        for summary in await self._sessions.sessions(tenant=tenant):
            if since is not None and _as_utc(summary.last_at) <= since:
                continue
            history = await self._sessions.history(tenant=tenant, session_id=summary.id)
            if history:
                out.append((summary.title, list(history[-MAX_MESSAGES_PER_SESSION:])))
            if len(out) >= self._max_sessions:
                break
        return out

    def _build_prompt(
        self,
        transcripts: list[tuple[str, list[tuple[str, str]]]],
        rejected: list[ReviewDecision],
    ) -> str:
        """The user turn: the recent transcripts, plus what the operator has already declined."""
        parts: list[str] = []
        if rejected:
            # Negative context first, so it frames the reading rather than trailing it
            # (ADR-0093 §6).
            declined = "\n\n".join(
                f"- {d.title} ({d.operation}): {d.proposed_content[:500]}" for d in rejected
            )
            parts.append(
                "The operator has already REJECTED these proposals. Do not propose them again "
                "unless the conversations below show the pattern has meaningfully changed:"
                f"\n\n{declined}"
            )
        convo = "\n\n".join(
            "\n".join([f"### Conversation: {title or 'untitled'}"] + [f"{r}: {c}" for r, c in msgs])
            for title, msgs in transcripts
        )
        parts.append(f"Recent conversations to learn from:\n\n{convo}")
        return "\n\n---\n\n".join(parts)

    async def _stage(self, *, tenant: str, content: str) -> int:
        """Parse the model's reply and stage each valid, non-duplicate proposal."""
        proposals = _parse_proposals(content)
        if not proposals:
            return 0
        known = {p.name for p in await self._playbooks.list_playbooks(tenant)}
        pending = {p.path for p in await self._proposals.list_pending(tenant=tenant)}
        staged = 0
        for raw in proposals:
            resolved = self._resolve(raw, known=known)
            if resolved is None:
                continue
            path, operation, body, note = resolved
            if path in pending:
                # Last night's proposal for this document is still awaiting the operator. Stacking
                # a second one would make the queue grow while they're away and force them to
                # resolve a stale draft to reach a fresher one.
                log.debug("skipping proposal; one is already pending", tenant=tenant, path=path)
                continue
            await self._proposals.add(
                tenant=tenant,
                path=path,
                operation=operation,
                proposed_content=body,
                origin="reflection",
                note=note,
            )
            pending.add(path)  # a reply naming the same document twice stages it once
            staged += 1
        if staged:
            log.info("staged playbook proposals for review", tenant=tenant, count=staged)
        return staged

    def _resolve(self, raw: dict[str, Any], *, known: set[str]) -> tuple[str, str, str, str] | None:
        """Validate one proposal into ``(path, operation, content, note)``, or ``None`` to drop it.

        The **operation is derived here, never taken from the model**: a playbook the tenant
        already has is an ``update``, otherwise a ``create``. Trusting the model's own word would
        mis-render the review — a "create" shows an empty *current* side, so a mislabelled update
        would hide from the operator exactly what their approval is about to overwrite.
        """
        body = str(raw.get("content") or "").strip()
        if not body or len(body) > MAX_PROPOSAL_CHARS:
            return None
        note = str(raw.get("note") or "").strip()[:500]
        target = str(raw.get("target") or "").strip().lower()
        if target == "instructions":
            # The base prompt always exists (stored, or the shipped default), so it is only ever
            # an update.
            return INSTRUCTIONS_PATH, "update", body, note
        if target != "playbook":
            return None
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        return playbook_path(name), ("update" if name in known else "create"), body, note


def _as_utc(value: datetime) -> datetime:
    """*value* as an aware UTC datetime — SQLite hands back naive ones, Postgres aware.

    Without this the watermark comparison raises on SQLite (naive vs. aware) — a difference the
    unit tests would surface and production would not, or vice versa.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_proposals(content: str) -> list[dict[str, Any]]:
    """The ``proposals`` array from the model's reply; ``[]` for anything unusable.

    Tolerates a fenced code block (models wrap JSON in ```json despite instructions). Junk is
    nothing to stage, not an error to raise: a bad generation should cost the operator nothing.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0].strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        log.debug("reflection reply was not JSON; nothing staged")
        return []
    if not isinstance(parsed, dict):
        return []
    items = parsed.get("proposals")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
