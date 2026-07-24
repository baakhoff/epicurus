"""The core's own ``automations`` review page — approve a conversationally-drafted automation.

The ``propose_automation`` built-in (#667) never creates an automation. It *stages* a proposal
here, exactly as the nightly reflection pass stages a playbook edit on the ``playbooks`` page
(ADR-0093 §2) — and this page is the **only** thing that writes an automation on the agent's
behalf. Approving is the consent that creates the automation *enabled*; rejecting leaves only an
audit row. Nothing self-applies: the tool has no path to :class:`AutomationStore.create`, and the
one code path that does — :meth:`CoreAutomationReviewPage.approve` — runs only from a staged,
operator-approved proposal. That is the hard guardrail, enforced by construction and tested.

The suggestion renders *understandably* rather than as a raw text diff (ADR-0107): each pending
proposal carries an :class:`~epicurus_core.review.AutomationPreview` — the trigger in words, the
filter, the action, the autonomy level, the sinks, and the drafted model — which the shell shows
with a **model picker** the operator can change before approving. That changed model comes back as
the approve ``content`` (the one field editable pre-approval); for an ``update`` the shell also
shows a readable before→after diff of those same human-readable lines.

Storage mirrors the playbook precedent (ADR-0090): the pending queue *is* the set of rows (a
resolved row leaves it), and a durable decision trail records what was proposed versus what was
actually applied.
"""

from __future__ import annotations

import difflib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import get_logger
from epicurus_core.manifest import PageSpec
from epicurus_core.review import (
    ApplyResult,
    AutomationPreview,
    ReviewAuditData,
    ReviewData,
    ReviewDecision,
    ReviewSuggestion,
)
from epicurus_core_app.automations.model import (
    AUTONOMY_LEVELS,
    Automation,
    AutonomyLevel,
    Cadence,
    EventTrigger,
    PayloadMatcher,
    ScheduleTrigger,
    Sink,
    validate_automation,
)
from epicurus_core_app.automations.store import AutomationStore

log = get_logger("epicurus_core_app.automations.review")

#: The core's automations review page id (the ``core`` pseudo-module's second page, ADR-0107).
#: ``/m/core/automations`` is its route; the shell folds it into the unified Suggestions inbox.
CORE_AUTOMATIONS_PAGE_ID = "automations"

#: The provenance stamped on an automation the agent drafts by conversation — so the Automations
#: page can tell it apart from one the operator built by hand or instantiated from a template.
PROPOSAL_SOURCE = "agent"

#: The valid matcher operators (mirrors :class:`PayloadMatcher`'s ``op`` Literal), checked here so
#: a bad op the model invents is a clear error to it, not a matcher that silently never fires.
_MATCHER_OPS = frozenset({"eq", "ne", "contains", "exists", "gt", "lt"})

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: The autonomy dial in words, for the preview and the readable diff. Kept beside the review page
#: rather than in the (deliberately pure) model vocabulary, since these are UI copy.
AUTONOMY_LABELS: dict[str, str] = {
    "notify": "Notify — look, don't touch",
    "propose": "Propose — may draft for approval",
    "act": "Act — may make changes directly",
    "silent_act": "Silent — acts, reports only to the run log",
}


# ── the staged draft ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProposedAutomation:
    """One drafted automation, staged for approval — the machine spec behind a suggestion.

    Immutable: :meth:`CoreAutomationReviewPage.approve` folds the operator's model choice in with
    :func:`dataclasses.replace`, never a mutation, so a re-render after a failed approve still
    shows exactly what was staged.
    """

    operation: str  # "create" | "update"
    name: str
    prompt: str
    autonomy: AutonomyLevel
    sinks: list[Sink]
    model: str | None
    event_trigger: EventTrigger | None
    schedule_trigger: ScheduleTrigger | None
    rate_cap_per_hour: int = 0
    digest_window_minutes: int = 0
    automation_id: str | None = None  # the row to edit, for operation == "update"

    def validate(self) -> None:
        """Raise ``ValueError`` if the draft is not a coherent, approvable automation.

        Runs the same :func:`validate_automation` a hand-built row passes, plus the checks that
        gate this path specifically: a non-blank action, known matcher operators, and an
        ``automation_id`` whenever the operation is an update.
        """
        if self.operation not in ("create", "update"):
            raise ValueError(f"operation must be 'create' or 'update', got {self.operation!r}")
        if self.operation == "update" and not (self.automation_id or "").strip():
            raise ValueError("operation 'update' needs the automation_id of the row to edit")
        if not self.prompt.strip():
            raise ValueError("action must not be blank — say what the automation should do")
        if self.event_trigger is not None:
            for matcher in self.event_trigger.matchers:
                if matcher.op not in _MATCHER_OPS:
                    raise ValueError(
                        f"unknown filter operator {matcher.op!r}; expected one of "
                        f"{sorted(_MATCHER_OPS)}"
                    )
        validate_automation(
            name=self.name,
            source=PROPOSAL_SOURCE,
            autonomy=self.autonomy,
            sinks=list(self.sinks),
            event_trigger=self.event_trigger,
            schedule_trigger=self.schedule_trigger,
            rate_cap_per_hour=self.rate_cap_per_hour,
            digest_window_minutes=self.digest_window_minutes,
        )

    def to_json(self) -> str:
        """Serialize for storage — the shape :meth:`from_json` reads back."""
        return json.dumps(
            {
                "operation": self.operation,
                "automation_id": self.automation_id,
                "name": self.name,
                "prompt": self.prompt,
                "autonomy": self.autonomy,
                "sinks": list(self.sinks),
                "model": self.model,
                "rate_cap_per_hour": self.rate_cap_per_hour,
                "digest_window_minutes": self.digest_window_minutes,
                "event_trigger": _event_to_json(self.event_trigger),
                "schedule_trigger": _schedule_to_json(self.schedule_trigger),
            }
        )

    @staticmethod
    def from_json(raw: str) -> ProposedAutomation:
        """Rebuild a draft from its stored JSON."""
        data: dict[str, Any] = json.loads(raw)
        return ProposedAutomation(
            operation=str(data.get("operation", "create")),
            automation_id=data.get("automation_id"),
            name=str(data.get("name", "")),
            prompt=str(data.get("prompt", "")),
            autonomy=_coerce_autonomy(data.get("autonomy")),
            sinks=[_coerce_sink(s) for s in data.get("sinks", [])],
            model=data.get("model"),
            rate_cap_per_hour=int(data.get("rate_cap_per_hour", 0) or 0),
            digest_window_minutes=int(data.get("digest_window_minutes", 0) or 0),
            event_trigger=_event_from_json(data.get("event_trigger")),
            schedule_trigger=_schedule_from_json(data.get("schedule_trigger")),
        )


# ── human rendering (preview + diff) ─────────────────────────────────────────


@dataclass(frozen=True)
class _Display:
    """The human-readable face of an automation, shared by the preview and the diff.

    Built the same way from a staged :class:`ProposedAutomation` and from a live
    :class:`Automation`, so an ``update``'s before→after diff compares like with like.
    """

    name: str
    trigger: str
    filter: str
    action: str
    autonomy: str
    sinks: list[str]
    model: str | None

    @staticmethod
    def from_draft(draft: ProposedAutomation) -> _Display:
        return _Display(
            name=draft.name,
            trigger=_render_trigger(draft.event_trigger, draft.schedule_trigger),
            filter=_render_filter(draft.event_trigger),
            action=draft.prompt,
            autonomy=draft.autonomy,
            sinks=list(draft.sinks),
            model=draft.model,
        )

    @staticmethod
    def from_automation(a: Automation) -> _Display:
        return _Display(
            name=a.name,
            trigger=_render_trigger(a.event_trigger, a.schedule_trigger),
            filter=_render_filter(a.event_trigger),
            action=a.prompt,
            autonomy=a.autonomy,
            sinks=list(a.sinks),
            model=a.model,
        )

    def to_preview(self) -> AutomationPreview:
        return AutomationPreview(
            name=self.name,
            trigger=self.trigger,
            filter=self.filter,
            action=self.action,
            autonomy=self.autonomy,
            autonomy_label=AUTONOMY_LABELS.get(self.autonomy, self.autonomy),
            sinks=list(self.sinks),
            model=self.model,
        )

    def to_text(self) -> str:
        """The multi-line rendering the diff and the audit trail compare and store."""
        return "\n".join(
            [
                f"Name: {self.name}",
                f"Trigger: {self.trigger}",
                f"Filter: {self.filter or '(none)'}",
                f"Autonomy: {AUTONOMY_LABELS.get(self.autonomy, self.autonomy)}",
                f"Sinks: {', '.join(self.sinks) or '(none)'}",
                f"Model: {self.model or '(operator default)'}",
                "Action:",
                self.action.strip(),
                "",
            ]
        )


def _render_trigger(event: EventTrigger | None, schedule: ScheduleTrigger | None) -> str:
    """The trigger in one plain sentence."""
    if schedule is not None:
        when = f"at {schedule.hour:02d}:00"
        if schedule.cadence == "weekly" and schedule.weekday is not None:
            day = _WEEKDAYS[schedule.weekday] if 0 <= schedule.weekday <= 6 else "?"
            return f"Every {day} {when}"
        return f"Every day {when}"
    if event is not None:
        base = f"When {event.module} emits {event.event_type}"
        if event.window_start_hour is not None and event.window_end_hour is not None:
            base += (
                f" (between {event.window_start_hour:02d}:00 and {event.window_end_hour:02d}:00)"
            )
        return base
    return "(no trigger)"


def _render_filter(event: EventTrigger | None) -> str:
    """The event filter in words, or ``""`` when there is none."""
    if event is None or not event.matchers:
        return ""
    parts: list[str] = []
    for m in event.matchers:
        if m.op == "exists":
            parts.append(f"{m.field} is present")
        else:
            symbol = {
                "eq": "=",
                "ne": "≠",
                "contains": "contains",
                "gt": ">",
                "lt": "<",
            }.get(m.op, m.op)
            parts.append(f"{m.field} {symbol} {m.value!r}")
    return ", ".join(parts)


def _unified_diff(before: str, after: str) -> str:
    """A readable unified diff of two human-rendered automations (empty ``before`` for a create)."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/automation",
            tofile="b/automation",
            n=3,
        )
    )


# ── trigger (de)serialization (the store's JSON shape, kept local) ────────────


def _event_to_json(t: EventTrigger | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {
        "module": t.module,
        "event_type": t.event_type,
        "matchers": [{"field": m.field, "op": m.op, "value": m.value} for m in t.matchers],
        "window_start_hour": t.window_start_hour,
        "window_end_hour": t.window_end_hour,
    }


def _event_from_json(data: dict[str, Any] | None) -> EventTrigger | None:
    if not data:
        return None
    return EventTrigger(
        module=str(data.get("module", "")),
        event_type=str(data.get("event_type", "")),
        matchers=[
            PayloadMatcher(field=str(m["field"]), op=m["op"], value=m.get("value"))
            for m in data.get("matchers", [])
        ],
        window_start_hour=_opt_int(data.get("window_start_hour")),
        window_end_hour=_opt_int(data.get("window_end_hour")),
    )


def _schedule_to_json(t: ScheduleTrigger | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {"cadence": t.cadence, "hour": t.hour, "weekday": t.weekday}


def _schedule_from_json(data: dict[str, Any] | None) -> ScheduleTrigger | None:
    if not data:
        return None
    raw = data.get("cadence", "daily")
    cadence: Cadence = cast("Cadence", raw) if raw in ("daily", "weekly") else "daily"
    return ScheduleTrigger(
        cadence=cadence,
        hour=int(data.get("hour", 0) or 0),
        weekday=_opt_int(data.get("weekday")),
    )


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_autonomy(value: Any) -> AutonomyLevel:
    text = str(value or "notify")
    if text not in AUTONOMY_LEVELS:
        return "notify"
    return text  # narrowed to AutonomyLevel by the membership check


def _coerce_sink(value: Any) -> Sink:
    # validate_automation is what rejects an unknown sink; this only narrows the type.
    return cast("Sink", str(value))


# ── storage (pending queue + decision trail, ADR-0090) ────────────────────────


@dataclass(frozen=True)
class StagedProposal:
    """One pending proposal — an immutable projection of a stored row."""

    sid: str
    path: str
    operation: str
    origin: str
    note: str
    draft: ProposedAutomation
    created_at: datetime


class _Base(DeclarativeBase):
    pass


class _StoredProposal(_Base):
    """One pending automation proposal, scoped to a tenant."""

    __tablename__ = "automation_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    sid: Mapped[str] = mapped_column(String(32), index=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    path: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200), default="")
    spec: Mapped[str] = mapped_column(Text, default="")  # ProposedAutomation.to_json()
    origin: Mapped[str] = mapped_column(String(64), default="conversation")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredDecision(_Base):
    """One resolved proposal: what was proposed alongside what was actually applied (ADR-0090)."""

    __tablename__ = "automation_review_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sid: Mapped[str] = mapped_column(String(32))
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    path: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(64), default="conversation")
    note: Mapped[str] = mapped_column(Text, default="")
    proposed_content: Mapped[str] = mapped_column(Text, default="")
    applied_content: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(16))
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


#: Retention for the resolved-decision trail, mirroring the module-side cap (ADR-0090).
MAX_DECISIONS = 200


class AutomationProposalStore:
    """Tenant-scoped pending queue + resolved-decision trail for automation proposals (ADR-0090)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def add(
        self,
        *,
        tenant: str,
        draft: ProposedAutomation,
        origin: str = "conversation",
        note: str = "",
    ) -> StagedProposal:
        """Stage a proposal and return it (with its freshly minted ``sid``)."""
        sid = uuid.uuid4().hex
        path = (
            f"automation/{draft.automation_id}"
            if draft.operation == "update" and draft.automation_id
            else "automation/new"
        )
        async with self._session() as session:
            row = _StoredProposal(
                sid=sid,
                tenant=tenant,
                path=path,
                operation=draft.operation,
                name=draft.name,
                spec=draft.to_json(),
                origin=origin,
                note=note,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _proposal_value(row)

    async def list_pending(self, *, tenant: str) -> list[StagedProposal]:
        """All pending proposals for *tenant*, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredProposal)
                .where(_StoredProposal.tenant == tenant)
                .order_by(_StoredProposal.created_at, _StoredProposal.id)
            )
            return [_proposal_value(row) for row in rows]

    async def get(self, *, tenant: str, sid: str) -> StagedProposal | None:
        """One pending proposal by its opaque ``sid``, or ``None``."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredProposal).where(
                    _StoredProposal.tenant == tenant, _StoredProposal.sid == sid
                )
            )
            return _proposal_value(row) if row is not None else None

    async def delete(self, *, tenant: str, sid: str) -> bool:
        """Drop a resolved proposal from the queue. True if a row was deleted."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredProposal).where(
                    _StoredProposal.tenant == tenant, _StoredProposal.sid == sid
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def record(
        self,
        *,
        tenant: str,
        staged: StagedProposal,
        decision: str,
        proposed_content: str,
        applied_content: str = "",
    ) -> None:
        """Record one resolved proposal, then prune beyond :data:`MAX_DECISIONS` (ADR-0090).

        Written **before** the pending row drops, so a crash between the two leaves an audited
        decision and a stale queue row (visible, re-resolvable) rather than a vanished proposal.
        """
        async with self._session() as session:
            session.add(
                _StoredDecision(
                    sid=staged.sid,
                    tenant=tenant,
                    path=staged.path,
                    operation=staged.operation,
                    title=staged.draft.name,
                    origin=staged.origin,
                    note=staged.note,
                    proposed_content=proposed_content,
                    applied_content=applied_content,
                    decision=decision,
                    proposed_at=staged.created_at,
                )
            )
            await session.commit()
            keep_ids = (
                await session.scalars(
                    select(_StoredDecision.id)
                    .where(_StoredDecision.tenant == tenant)
                    .order_by(_StoredDecision.id.desc())
                    .limit(MAX_DECISIONS)
                )
            ).all()
            await session.execute(
                delete(_StoredDecision).where(
                    _StoredDecision.tenant == tenant,
                    _StoredDecision.id.notin_(keep_ids),
                )
            )
            await session.commit()

    async def decisions(self, *, tenant: str, limit: int = 50) -> list[ReviewDecision]:
        """The resolved-decision trail, newest first (ADR-0090)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredDecision)
                .where(_StoredDecision.tenant == tenant)
                .order_by(_StoredDecision.id.desc())
                .limit(limit)
            )
            return [
                ReviewDecision(
                    id=row.sid,
                    title=row.title,
                    path=row.path,
                    operation=row.operation,
                    origin=row.origin,
                    note=row.note,
                    created_at=row.proposed_at.isoformat(),
                    decided_at=row.decided_at.isoformat(),
                    decision=row.decision,
                    proposed_content=row.proposed_content,
                    applied_content=row.applied_content,
                )
                for row in rows
            ]


def _proposal_value(row: _StoredProposal) -> StagedProposal:
    return StagedProposal(
        sid=row.sid,
        path=row.path,
        operation=row.operation,
        origin=row.origin,
        note=row.note,
        draft=ProposedAutomation.from_json(row.spec),
        created_at=row.created_at,
    )


# ── the in-process review page ────────────────────────────────────────────────


class CoreAutomationReviewPage:
    """The ``core`` pseudo-module's second ``review`` page: staged automations (ADR-0107).

    Same surface a real module (or the sibling playbooks page) serves — :meth:`page_spec`,
    :meth:`get_page`, :meth:`review_action`, :meth:`review_audit` — so :class:`CorePages` fans
    out to it exactly like any other, and the shell cannot tell the difference.
    """

    def __init__(
        self,
        *,
        store: AutomationProposalStore,
        automations: AutomationStore,
        tenant: str,
    ) -> None:
        self._store = store
        self._automations = automations
        self._tenant = tenant

    def page_spec(self) -> PageSpec:
        """The nav/review descriptor for this page."""
        return PageSpec(
            id=CORE_AUTOMATIONS_PAGE_ID,
            title="Automations",
            archetype="review",
            icon="zap",
        )

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """The ``review`` archetype's data: the pending queue, each with a preview + diff."""
        self._assert_page(page_id)
        data = await self.list_review()
        return data.model_dump()

    async def list_review(self) -> ReviewData:
        """The pending queue: an :class:`AutomationPreview` each, plus a readable diff for edits."""
        items: list[ReviewSuggestion] = []
        for staged in await self._store.list_pending(tenant=self._tenant):
            proposed = _Display.from_draft(staged.draft)
            current_text = await self._current_text(staged.draft)
            items.append(
                ReviewSuggestion(
                    id=staged.sid,
                    title=staged.draft.name,
                    path=staged.path,
                    operation=staged.operation,
                    origin=staged.origin,
                    note=staged.note,
                    created_at=staged.created_at.isoformat(),
                    diff=_unified_diff(current_text, proposed.to_text()),
                    automation=proposed.to_preview(),
                )
            )
        return ReviewData(title="Automations", suggestions=items)

    async def review_action(
        self, page_id: str, suggestion_id: str, action: str, content: str | None = None
    ) -> dict[str, Any]:
        """Approve (create/update the automation) or reject (discard) one staged proposal."""
        self._assert_page(page_id)
        if action == "approve":
            result = await self.approve(suggestion_id, content)
        elif action == "reject":
            result = await self.reject(suggestion_id)
        else:
            raise HTTPException(status_code=404, detail=f"unknown review action: {action}")
        return result.model_dump()

    async def review_audit(self, page_id: str, *, limit: int = 50) -> dict[str, Any]:
        """The resolved-decision trail for this page, newest first (ADR-0090)."""
        self._assert_page(page_id)
        decisions = await self._store.decisions(tenant=self._tenant, limit=limit)
        return ReviewAuditData(decisions=decisions).model_dump()

    async def approve(self, sid: str, content: str | None = None) -> ApplyResult:
        """Create (or update) the automation — *enabled*. Approval is the consent (#667).

        *content* is the operator's model choice from the picker (ADR-0107): the automations page
        renders exactly one editable control, the model, and its value arrives here as ``content``
        — ``""`` meaning the tenant default, a name meaning that model, and ``None`` (a
        quick-approve from the suggestions list, no edit) meaning the drafted model stands. The
        draft is re-validated after the swap, so a picker value can never stage a bad row.
        """
        staged = await self._store.get(tenant=self._tenant, sid=sid)
        if staged is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        draft = staged.draft
        if content is not None:
            draft = replace(draft, model=(content.strip() or None))
        try:
            draft.validate()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        applied = await self._apply(draft)
        await self._store.record(
            tenant=self._tenant,
            staged=staged,
            decision="approved",
            proposed_content=_Display.from_draft(draft).to_text(),
            applied_content=_Display.from_automation(applied).to_text(),
        )
        await self._store.delete(tenant=self._tenant, sid=sid)
        return ApplyResult(id=sid, status="approved", path=staged.path, operation=staged.operation)

    async def reject(self, sid: str) -> ApplyResult:
        """Discard a proposal, recording the rejection in the audit trail (ADR-0090)."""
        staged = await self._store.get(tenant=self._tenant, sid=sid)
        if staged is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        await self._store.record(
            tenant=self._tenant,
            staged=staged,
            decision="rejected",
            proposed_content=_Display.from_draft(staged.draft).to_text(),
        )
        await self._store.delete(tenant=self._tenant, sid=sid)
        return ApplyResult(id=sid, status="rejected", path=staged.path, operation=staged.operation)

    async def _apply(self, draft: ProposedAutomation) -> Automation:
        """Write the automation through :class:`AutomationStore` — the one place that does.

        A ``create`` lands *enabled* (approval is the consent). An ``update`` preserves the row's
        ``enabled`` flag and chat continuity (its ``chat_mode``/session are runtime state, not part
        of what was proposed), and 409s if the row was deleted between staging and approval —
        applying an edit to a vanished automation would resurrect it, which the operator did not
        ask for.
        """
        if draft.operation == "update":
            automation_id = draft.automation_id or ""
            existing = await self._automations.get(tenant=self._tenant, automation_id=automation_id)
            if existing is None:
                raise HTTPException(
                    status_code=409,
                    detail="that automation no longer exists; reject this proposal instead",
                )
            updated = await self._automations.update(
                tenant=self._tenant,
                automation_id=automation_id,
                name=draft.name,
                prompt=draft.prompt,
                autonomy=draft.autonomy,
                event_trigger=draft.event_trigger,
                schedule_trigger=draft.schedule_trigger,
                model=draft.model,
                sinks=list(draft.sinks),
                chat_mode=existing.chat_mode,
                rate_cap_per_hour=draft.rate_cap_per_hour,
                digest_window_minutes=draft.digest_window_minutes,
                enabled=existing.enabled,
            )
            if updated is None:  # a delete raced the approval — same intent as the check above
                raise HTTPException(
                    status_code=409,
                    detail="that automation no longer exists; reject this proposal instead",
                )
            return updated
        return await self._automations.create(
            tenant=self._tenant,
            name=draft.name,
            prompt=draft.prompt,
            autonomy=draft.autonomy,
            source=PROPOSAL_SOURCE,
            event_trigger=draft.event_trigger,
            schedule_trigger=draft.schedule_trigger,
            model=draft.model,
            sinks=list(draft.sinks),
            rate_cap_per_hour=draft.rate_cap_per_hour,
            digest_window_minutes=draft.digest_window_minutes,
            enabled=True,
        )

    async def _current_text(self, draft: ProposedAutomation) -> str:
        """The live automation's rendering for an ``update`` diff base, or ``""`` for a create."""
        if draft.operation != "update" or not draft.automation_id:
            return ""
        existing = await self._automations.get(
            tenant=self._tenant, automation_id=draft.automation_id
        )
        return _Display.from_automation(existing).to_text() if existing is not None else ""

    @staticmethod
    def _assert_page(page_id: str) -> None:
        if page_id != CORE_AUTOMATIONS_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"core has no automations page {page_id!r}")


# ── the propose_automation built-in tool (#667) ───────────────────────────────
#
# Spec + handler live here, next to the store and the guardrail, rather than in agent/builtins.py
# with the generic tools: everything about *how a proposal is created and staged* is one file, and
# builtins.py keeps no dependency on the automations package.

#: The OpenAI-style function spec the gateway sends the model.
PROPOSE_AUTOMATION_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_automation",
        "description": (
            "Draft an automation and stage it for the operator to approve. Use this when the user "
            "asks for something recurring or event-driven — 'when I get mail from my boss, notify "
            "me', 'every Monday at 9am summarize last week'. Call it once per automation: for two "
            "separate pipelines, call it twice. You never create or switch on an automation "
            "yourself — you stage a proposal the operator reviews (they can change the model) and "
            "approves. After calling it, tell the user you've staged it for their approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "A short human name, e.g. 'Important mail alerts'.",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "What the assistant should do each time it runs, as plain instructions — "
                        "e.g. 'Summarize the new mail and tell me what needs a reply.'"
                    ),
                },
                "autonomy": {
                    "type": "string",
                    "enum": list(AUTONOMY_LEVELS),
                    "description": (
                        "How much a run may do: 'notify' (read-only, just reports), "
                        "'propose' (may draft things for approval, e.g. compose a reply), "
                        "'act' (may make changes directly), 'silent_act' (acts but reports "
                        "only to the run log). Default to 'notify' unless the user clearly "
                        "wants changes made on their behalf."
                    ),
                },
                "sinks": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["push", "chat", "notes", "kb"]},
                    "description": (
                        "Where each run's result goes: 'push' (a notification), "
                        "'chat' (a conversation the user can reply in), 'notes' or 'kb' "
                        "(save to a document). Omit for a sensible default of a push "
                        "notification. Only add 'chat' if the user wants a conversation "
                        "thread they can reply in."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": "Optional specific model to run it with; omit for the default.",
                },
                "event_trigger": {
                    "type": "object",
                    "description": (
                        "Fire when a module emits an event. Provide this OR "
                        "schedule_trigger, never both."
                    ),
                    "properties": {
                        "module": {
                            "type": "string",
                            "description": "The module that emits it, e.g. 'mail', 'calendar'.",
                        },
                        "event_type": {
                            "type": "string",
                            "description": "The event type, e.g. 'mail.received'.",
                        },
                        "matchers": {
                            "type": "array",
                            "description": (
                                "Conditions on the event that must ALL hold for it to fire (AND). "
                                "Omit to fire on every event of this type."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {
                                        "type": "string",
                                        "description": "A top-level field of the event payload.",
                                    },
                                    "op": {
                                        "type": "string",
                                        "enum": sorted(_MATCHER_OPS),
                                    },
                                    "value": {
                                        "description": "Value to compare (omit for 'exists').",
                                    },
                                },
                                "required": ["field", "op"],
                            },
                        },
                        "window_start_hour": {
                            "type": "integer",
                            "description": "Optional: only fire at/after this local hour (0-23).",
                        },
                        "window_end_hour": {
                            "type": "integer",
                            "description": "Optional: only fire before this local hour (0-23).",
                        },
                    },
                    "required": ["module", "event_type"],
                },
                "schedule_trigger": {
                    "type": "object",
                    "description": (
                        "Fire on a schedule. Provide this OR event_trigger, never both."
                    ),
                    "properties": {
                        "cadence": {"type": "string", "enum": ["daily", "weekly"]},
                        "hour": {"type": "integer", "description": "Local hour, 0-23."},
                        "weekday": {
                            "type": "integer",
                            "description": "0=Monday..6=Sunday; required for a weekly cadence.",
                        },
                    },
                    "required": ["cadence", "hour"],
                },
                "operation": {
                    "type": "string",
                    "enum": ["create", "update"],
                    "description": (
                        "'create' (default) a new automation, or 'update' an existing one — pass "
                        "its automation_id."
                    ),
                },
                "automation_id": {
                    "type": "string",
                    "description": "Required for an 'update': the id of the row to edit.",
                },
                "digest_window_minutes": {
                    "type": "integer",
                    "description": (
                        "Optional: batch events arriving within this many minutes into one run "
                        "(0 = run per event)."
                    ),
                },
                "rate_cap_per_hour": {
                    "type": "integer",
                    "description": "Optional: cap runs per hour (0 = uncapped).",
                },
            },
            "required": ["name", "action", "autonomy"],
        },
    },
}


BuiltinHandler = Callable[[dict[str, Any], str], Awaitable[str]]


def _draft_from_args(arguments: dict[str, Any]) -> ProposedAutomation:
    """Build a :class:`ProposedAutomation` from the model's tool arguments (unvalidated)."""
    event = arguments.get("event_trigger")
    schedule = arguments.get("schedule_trigger")
    sinks = arguments.get("sinks")
    autonomy = _coerce_autonomy(arguments.get("autonomy"))
    # A notify automation with no sink would run silently to the ledger; default to a push so the
    # common "notify me when…" ask actually reaches the operator. silent_act means "no sink" by
    # design, so it is left empty. The chat sink is never a default (owner rule, #672).
    default_sinks: list[Sink] = [] if autonomy == "silent_act" else ["push"]
    return ProposedAutomation(
        operation=str(arguments.get("operation") or "create"),
        automation_id=(str(arguments["automation_id"]) if arguments.get("automation_id") else None),
        name=str(arguments.get("name") or "").strip(),
        prompt=str(arguments.get("action") or "").strip(),
        autonomy=autonomy,
        sinks=[_coerce_sink(s) for s in sinks] if sinks else default_sinks,
        model=(str(arguments["model"]).strip() or None) if arguments.get("model") else None,
        event_trigger=_event_from_json(event if isinstance(event, dict) else None),
        schedule_trigger=_schedule_from_json(schedule if isinstance(schedule, dict) else None),
        rate_cap_per_hour=int(arguments.get("rate_cap_per_hour") or 0),
        digest_window_minutes=int(arguments.get("digest_window_minutes") or 0),
    )


def make_propose_automation_handler(
    proposals: AutomationProposalStore,
    automations: AutomationStore,
) -> BuiltinHandler:
    """Build the ``propose_automation`` handler (#667).

    The handler **only stages** — it has no path to :class:`AutomationStore.create`/``update``.
    Its single read of the automation store is the existence check an ``update`` needs, so it never
    proposes an edit to a row that isn't there. Every failure is returned to the model as an
    ``error:`` string (so it can correct the draft), never raised — a bad draft must not break the
    chat turn. Tenant-scoped: the calling tenant stages under itself (constraint #1).
    """

    async def handler(arguments: dict[str, Any], tenant: str) -> str:
        try:
            draft = _draft_from_args(arguments)
            draft.validate()
        except ValueError as exc:
            return f"error: {exc}"
        except Exception as exc:  # a malformed argument shape — surface, don't crash the turn
            log.warning("propose_automation draft build failed", error=str(exc))
            return f"error: could not read that automation draft: {exc}"
        if draft.operation == "update":
            existing = await automations.get(tenant=tenant, automation_id=draft.automation_id or "")
            if existing is None:
                return (
                    f"error: no automation with id {draft.automation_id!r} to update — list the "
                    "automations first to find the right id."
                )
        try:
            await proposals.add(tenant=tenant, draft=draft, origin="conversation")
        except Exception as exc:  # a store hiccup must not crash the turn
            log.warning("propose_automation staging failed", error=str(exc))
            return f"error: could not stage that automation for approval: {exc}"
        what = "an edit to" if draft.operation == "update" else "a new automation"
        return (
            f"Staged {what} '{draft.name}' for your approval. It won't run until you approve it on "
            "the Suggestions page — where you can change the model first."
        )

    return handler


__all__ = [
    "AUTONOMY_LABELS",
    "CORE_AUTOMATIONS_PAGE_ID",
    "PROPOSAL_SOURCE",
    "PROPOSE_AUTOMATION_SPEC",
    "AutomationProposalStore",
    "CoreAutomationReviewPage",
    "ProposedAutomation",
    "StagedProposal",
    "make_propose_automation_handler",
]
