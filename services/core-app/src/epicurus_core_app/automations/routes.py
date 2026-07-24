"""Automations HTTP surface — CRUD, the run ledger, the kill switch, templates.

Core-owned Settings/page territory (ADR-0018), the same shape as the scheduled-turns and
timezone routes — not a `pages` archetype any module declares. The Automations page itself
is a companion issue (#668); this ships the data it will render, so that issue is a UI
change rather than a UI *and* an API change.

``POST /platform/v1/automations/{id}/run`` is deliberately here: an automation you cannot
try is an automation you cannot trust, and "wait until 7am to find out if the prompt was
any good" is not a development loop.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from epicurus_core import EntityRef
from epicurus_core_app.automations.feed import RunFeed, valid_outcome
from epicurus_core_app.automations.model import (
    AUTONOMY_LEVELS,
    SINKS,
    Automation,
    AutomationRun,
    DocumentTarget,
    EventTrigger,
    PayloadMatcher,
    ScheduleTrigger,
    validate_automation,
)
from epicurus_core_app.automations.runner import AutomationRunner
from epicurus_core_app.automations.store import AutomationStore, KillSwitchStore
from epicurus_core_app.event_log import EventLogStore

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

# ── wire shapes ──────────────────────────────────────────────────────────────


class MatcherBody(BaseModel):
    field: str
    op: Literal["eq", "ne", "contains", "exists", "gt", "lt"]
    value: Any = None


class EventTriggerBody(BaseModel):
    module: str
    event_type: str
    matchers: list[MatcherBody] = Field(default_factory=list)
    window_start_hour: int | None = None
    window_end_hour: int | None = None


class ScheduleTriggerBody(BaseModel):
    cadence: str
    hour: int
    weekday: int | None = None


class SinkTargetBody(BaseModel):
    """Where a notes/kb sink writes (#672): a document path pattern + create-vs-append.

    ``path_pattern`` is the module-relative path with ``{date}`` / ``{datetime}`` / ``{time}``
    substituted at run time (e.g. ``"Automations/Mail report {date}"``).
    """

    path_pattern: str
    mode: Literal["create", "append"] = "append"


class AutomationView(BaseModel):
    """The API-facing shape of an automation."""

    id: str
    name: str
    enabled: bool
    source: str
    event_trigger: EventTriggerBody | None = None
    schedule_trigger: ScheduleTriggerBody | None = None
    prompt: str
    model: str | None = None
    autonomy: str
    sinks: list[str]
    chat_mode: str
    rate_cap_per_hour: int
    digest_window_minutes: int
    #: The notes/kb document targets (#672); null when that sink is not configured.
    notes_target: SinkTargetBody | None = None
    kb_target: SinkTargetBody | None = None
    created_at: str
    last_run_at: str | None = None
    last_status: str | None = None
    #: What this automation's turns may actually do — derived, never stored, so the UI
    #: shows the same allowance the tool surface enforces rather than its own guess.
    allowed_tool_classes: list[str]


class AutomationRunView(BaseModel):
    """One ledger entry, as the runs feed surfaces it."""

    id: str
    automation_id: str
    started_at: str
    trigger_refs: list[int]
    filter_verdict: str
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
    outcome: str
    error: str | None = None
    output: str
    sinks_fired: list[str]
    #: The triggering events' entity refs (#669), resolved from the event log by row id so
    #: the feed renders source-entity hover-card chips with no per-module code. Empty for
    #: schedule/manual runs, and for trigger events retention has since pruned.
    trigger_entity_refs: list[EntityRef] = Field(default_factory=list)
    #: Documents this run produced via the notes/kb sinks (#672) — rendered as chips linking to
    #: what was written, the same way ``trigger_entity_refs`` renders the source.
    artifacts: list[EntityRef] = Field(default_factory=list)


class CreateAutomationRequest(BaseModel):
    name: str
    prompt: str
    autonomy: str = "notify"
    source: str = "user"
    event_trigger: EventTriggerBody | None = None
    schedule_trigger: ScheduleTriggerBody | None = None
    model: str | None = None
    sinks: list[str] = Field(default_factory=list)
    chat_mode: Literal["rolling", "per_run"] = "rolling"
    rate_cap_per_hour: int = 0
    digest_window_minutes: int = 0
    notes_target: SinkTargetBody | None = None
    kb_target: SinkTargetBody | None = None


class UpdateAutomationRequest(BaseModel):
    """The Automations page's save (#668) — every editable field, in one write.

    ``source`` is deliberately absent (provenance is not editable), and ``enabled`` rides
    along so the editor's toggle and its Save are one consistent state.
    """

    name: str
    prompt: str
    autonomy: str = "notify"
    event_trigger: EventTriggerBody | None = None
    schedule_trigger: ScheduleTriggerBody | None = None
    model: str | None = None
    sinks: list[str] = Field(default_factory=list)
    chat_mode: Literal["rolling", "per_run"] = "rolling"
    rate_cap_per_hour: int = 0
    digest_window_minutes: int = 0
    notes_target: SinkTargetBody | None = None
    kb_target: SinkTargetBody | None = None
    enabled: bool = True


class SetEnabledBody(BaseModel):
    enabled: bool


class KillSwitchBody(BaseModel):
    halted: bool


class TemplateView(BaseModel):
    """A module's preset automation, offered on the Templates tab."""

    module: str
    key: str
    name: str
    description: str = ""
    trigger: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    autonomy: str = "notify"
    sinks: list[str] = Field(default_factory=list)


# ── mapping ──────────────────────────────────────────────────────────────────


def _event_body(trigger: EventTrigger) -> EventTriggerBody:
    return EventTriggerBody(
        module=trigger.module,
        event_type=trigger.event_type,
        matchers=[MatcherBody(field=m.field, op=m.op, value=m.value) for m in trigger.matchers],
        window_start_hour=trigger.window_start_hour,
        window_end_hour=trigger.window_end_hour,
    )


def _to_event_trigger(body: EventTriggerBody) -> EventTrigger:
    return EventTrigger(
        module=body.module,
        event_type=body.event_type,
        matchers=[PayloadMatcher(field=m.field, op=m.op, value=m.value) for m in body.matchers],
        window_start_hour=body.window_start_hour,
        window_end_hour=body.window_end_hour,
    )


def _target_body(target: DocumentTarget | None) -> SinkTargetBody | None:
    if target is None:
        return None
    return SinkTargetBody(path_pattern=target.path_pattern, mode=target.mode)


def _to_target(body: SinkTargetBody | None) -> DocumentTarget | None:
    if body is None or not body.path_pattern.strip():
        return None
    return DocumentTarget(path_pattern=body.path_pattern.strip(), mode=body.mode)


def _require_targets(
    sinks: list[str], notes: DocumentTarget | None, kb: DocumentTarget | None
) -> None:
    """400 if a notes/kb sink is enabled without a document target to write into (#672)."""
    if "notes" in sinks and notes is None:
        raise HTTPException(status_code=400, detail="the notes sink needs a document target")
    if "kb" in sinks and kb is None:
        raise HTTPException(status_code=400, detail="the kb sink needs a document target")


def _view(automation: Automation) -> AutomationView:
    return AutomationView(
        id=automation.id,
        name=automation.name,
        enabled=automation.enabled,
        source=automation.source,
        event_trigger=(_event_body(automation.event_trigger) if automation.event_trigger else None),
        schedule_trigger=(
            ScheduleTriggerBody(
                cadence=automation.schedule_trigger.cadence,
                hour=automation.schedule_trigger.hour,
                weekday=automation.schedule_trigger.weekday,
            )
            if automation.schedule_trigger
            else None
        ),
        prompt=automation.prompt,
        model=automation.model,
        autonomy=automation.autonomy,
        sinks=list(automation.sinks),
        chat_mode=automation.chat_mode,
        rate_cap_per_hour=automation.rate_cap_per_hour,
        digest_window_minutes=automation.digest_window_minutes,
        notes_target=_target_body(automation.notes_target),
        kb_target=_target_body(automation.kb_target),
        created_at=automation.created_at.isoformat(),
        last_run_at=automation.last_run_at.isoformat() if automation.last_run_at else None,
        last_status=automation.last_status,
        allowed_tool_classes=sorted(automation.allowed()),
    )


def _run_view(
    run: AutomationRun, *, trigger_entity_refs: list[EntityRef] | None = None
) -> AutomationRunView:
    return AutomationRunView(
        id=run.id,
        automation_id=run.automation_id,
        started_at=run.started_at.isoformat(),
        trigger_refs=list(run.trigger_refs),
        filter_verdict=run.filter_verdict,
        model=run.model,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        duration_ms=run.duration_ms,
        outcome=run.outcome,
        error=run.error,
        output=run.output,
        sinks_fired=list(run.sinks_fired),
        trigger_entity_refs=trigger_entity_refs or [],
        artifacts=list(run.artifacts),
    )


async def _trigger_refs_for(
    events: EventLogStore | None, *, tenant: str, run: AutomationRun
) -> list[EntityRef]:
    """The distinct entity refs of *run*'s triggering events, in event order (#669).

    Best-effort: no event-log store wired, an empty ``trigger_refs`` (schedule/manual
    runs), or pruned rows all yield ``[]`` — the feed row then simply shows no chips.
    """
    if events is None or not run.trigger_refs:
        return []
    try:
        logged = await events.by_ids(tenant=tenant, ids=list(run.trigger_refs))
    except Exception:  # enrichment must never fail the feed
        return []
    refs: list[EntityRef] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in sorted(logged, key=lambda e: e.id):
        if entry.entity_ref is None:
            continue
        key = (entry.entity_ref.module, entry.entity_ref.kind, entry.entity_ref.ref_id)
        if key in seen:  # a digest run may batch several events about one entity
            continue
        seen.add(key)
        refs.append(entry.entity_ref)
    return refs


def create_automations_router(
    store: AutomationStore,
    kill_switch: KillSwitchStore,
    runner: AutomationRunner,
    *,
    templates: Any = None,
    default_tenant: str = "local",
    feed: RunFeed | None = None,
    events: EventLogStore | None = None,
) -> APIRouter:
    """CRUD + ledger + kill switch + templates (Settings surface, no module page).

    ``templates`` is the module registry's ``automation_templates`` lookup, injected as a
    bare callable so this router never imports the registry. *feed* backs the live runs
    tail (#669; without it ``GET /runs/stream`` 404s), and *events* resolves each run's
    ``trigger_refs`` into entity-ref chips (without it rows carry no chips).
    """
    router = APIRouter(prefix="/platform/v1/automations", tags=["automations"])

    @router.get("/vocabulary", response_model=dict[str, list[str]])
    async def vocabulary() -> dict[str, list[str]]:
        """The closed vocabularies the UI renders, so it never hardcodes them."""
        return {
            "autonomy_levels": list(AUTONOMY_LEVELS),
            "sinks": list(SINKS),
            "matcher_ops": ["eq", "ne", "contains", "exists", "gt", "lt"],
        }

    @router.get("/templates", response_model=list[TemplateView])
    async def list_templates() -> list[TemplateView]:
        """Every enabled module's preset automations — never auto-instantiated."""
        if templates is None:
            return []
        return [
            TemplateView(
                module=module,
                key=t.key,
                name=t.name,
                description=t.description,
                trigger=t.trigger,
                prompt=t.prompt,
                autonomy=t.autonomy,
                sinks=list(t.sinks),
            )
            for module, t in await templates()
        ]

    @router.get("/kill-switch", response_model=KillSwitchBody)
    async def get_kill_switch(tenant_id: str | None = Query(None)) -> KillSwitchBody:
        halted = await kill_switch.halted(tenant=tenant_id or default_tenant)
        return KillSwitchBody(halted=halted)

    @router.put("/kill-switch", response_model=KillSwitchBody)
    async def set_kill_switch(
        body: KillSwitchBody, tenant_id: str | None = Query(None)
    ) -> KillSwitchBody:
        """Stop or resume **every** automation for the tenant.

        Persisted, unlike the runtime power pause: a stop that a restart silently undoes is
        not a stop.
        """
        await kill_switch.set_halted(tenant=tenant_id or default_tenant, halted=body.halted)
        return body

    @router.get("/runs", response_model=list[AutomationRunView])
    async def list_all_runs(
        tenant_id: str | None = Query(None),
        automation_id: str | None = Query(None),
        outcome: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[AutomationRunView]:
        """The run ledger, newest first — what the runs feed renders.

        ``outcome`` narrows to one ledger state (``ok`` / ``error`` / ``skipped``);
        skipped runs are first-class here — a rate-capped or paused run being visible
        is the tab's whole point (#669).
        """
        if outcome is not None and not valid_outcome(outcome):
            raise HTTPException(status_code=400, detail=f"unknown outcome: {outcome!r}")
        tenant = tenant_id or default_tenant
        runs = await store.runs(
            tenant=tenant, automation_id=automation_id, outcome=outcome, limit=limit
        )
        return [
            _run_view(r, trigger_entity_refs=await _trigger_refs_for(events, tenant=tenant, run=r))
            for r in runs
        ]

    @router.get("/runs/stream")
    async def run_stream(
        tenant_id: str | None = Query(None),
        automation_id: str | None = Query(None),
        outcome: str | None = Query(None),
    ) -> StreamingResponse:
        """Tail the run ledger as SSE (#669) — the ADR-0031 console pattern.

        Each frame is ``event: automation_run`` with an ``AutomationRunView`` JSON body.
        Recent history replays oldest-first, then live runs follow as the runner records
        them — skips included. Filters apply server-side, matching ``GET /runs``.
        Client-disconnect handling matches the events feed: the generator's 1-second
        live-queue poll lets ``StreamingResponse`` notice a closed tab within ~1s.
        """
        if feed is None:
            raise HTTPException(status_code=404, detail="the runs feed is not wired")
        if outcome is not None and not valid_outcome(outcome):
            raise HTTPException(status_code=400, detail=f"unknown outcome: {outcome!r}")
        tenant = tenant_id or default_tenant

        async def frames() -> AsyncGenerator[str, None]:
            async for run in feed.stream(
                tenant=tenant, automation_id=automation_id, outcome=outcome
            ):
                view = _run_view(
                    run,
                    trigger_entity_refs=await _trigger_refs_for(events, tenant=tenant, run=run),
                )
                yield f"event: automation_run\ndata: {view.model_dump_json()}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream", headers=_SSE_HEADERS)

    @router.get("", response_model=list[AutomationView])
    async def list_automations(tenant_id: str | None = Query(None)) -> list[AutomationView]:
        return [_view(a) for a in await store.list(tenant=tenant_id or default_tenant)]

    @router.post("", response_model=AutomationView)
    async def create_automation(
        body: CreateAutomationRequest, tenant_id: str | None = Query(None)
    ) -> AutomationView:
        event_trigger = _to_event_trigger(body.event_trigger) if body.event_trigger else None
        schedule_trigger = (
            ScheduleTrigger(
                cadence=body.schedule_trigger.cadence,  # type: ignore[arg-type]  # validated below
                hour=body.schedule_trigger.hour,
                weekday=body.schedule_trigger.weekday,
            )
            if body.schedule_trigger
            else None
        )
        try:
            validate_automation(
                name=body.name,
                source=body.source,
                autonomy=body.autonomy,
                sinks=body.sinks,
                event_trigger=event_trigger,
                schedule_trigger=schedule_trigger,
                rate_cap_per_hour=body.rate_cap_per_hour,
                digest_window_minutes=body.digest_window_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        notes_target = _to_target(body.notes_target)
        kb_target = _to_target(body.kb_target)
        _require_targets(body.sinks, notes_target, kb_target)
        automation = await store.create(
            tenant=tenant_id or default_tenant,
            name=body.name.strip(),
            prompt=body.prompt,
            autonomy=body.autonomy,  # type: ignore[arg-type]  # validated above
            source=body.source,
            event_trigger=event_trigger,
            schedule_trigger=schedule_trigger,
            model=body.model,
            sinks=list(body.sinks),  # type: ignore[arg-type]  # validated above
            chat_mode=body.chat_mode,
            rate_cap_per_hour=body.rate_cap_per_hour,
            digest_window_minutes=body.digest_window_minutes,
            notes_target=notes_target,
            kb_target=kb_target,
        )
        return _view(automation)

    @router.put("/{automation_id}", response_model=AutomationView)
    async def update_automation(
        automation_id: str, body: UpdateAutomationRequest, tenant_id: str | None = Query(None)
    ) -> AutomationView:
        """Replace an automation's editable fields — the Automations page's save (#668).

        Same validation as create (**400** on an incoherent shape, before any write), so a
        rejected edit leaves the stored row untouched. ``source`` is deliberately not in
        the body: provenance is not editable — an instantiated template stays
        ``template:<module>``. **404** if unknown.
        """
        tenant = tenant_id or default_tenant
        current = await store.get(tenant=tenant, automation_id=automation_id)
        if current is None:
            raise HTTPException(status_code=404, detail="no such automation")
        event_trigger = _to_event_trigger(body.event_trigger) if body.event_trigger else None
        schedule_trigger = (
            ScheduleTrigger(
                cadence=body.schedule_trigger.cadence,  # type: ignore[arg-type]  # validated below
                hour=body.schedule_trigger.hour,
                weekday=body.schedule_trigger.weekday,
            )
            if body.schedule_trigger
            else None
        )
        try:
            validate_automation(
                name=body.name,
                source=current.source,  # provenance is kept, but still shape-checked
                autonomy=body.autonomy,
                sinks=body.sinks,
                event_trigger=event_trigger,
                schedule_trigger=schedule_trigger,
                rate_cap_per_hour=body.rate_cap_per_hour,
                digest_window_minutes=body.digest_window_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        notes_target = _to_target(body.notes_target)
        kb_target = _to_target(body.kb_target)
        _require_targets(body.sinks, notes_target, kb_target)
        updated = await store.update(
            tenant=tenant,
            automation_id=automation_id,
            name=body.name.strip(),
            prompt=body.prompt,
            autonomy=body.autonomy,  # type: ignore[arg-type]  # validated above
            event_trigger=event_trigger,
            schedule_trigger=schedule_trigger,
            model=body.model,
            sinks=list(body.sinks),  # type: ignore[arg-type]  # validated above
            chat_mode=body.chat_mode,
            rate_cap_per_hour=body.rate_cap_per_hour,
            digest_window_minutes=body.digest_window_minutes,
            notes_target=notes_target,
            kb_target=kb_target,
            enabled=body.enabled,
        )
        if updated is None:  # deleted between the read and the write — still a 404
            raise HTTPException(status_code=404, detail="no such automation")
        return _view(updated)

    @router.post("/{automation_id}/enabled", response_model=dict[str, object])
    async def set_enabled(
        automation_id: str, body: SetEnabledBody, tenant_id: str | None = Query(None)
    ) -> dict[str, object]:
        ok = await store.set_enabled(
            tenant=tenant_id or default_tenant, automation_id=automation_id, enabled=body.enabled
        )
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such automation: {automation_id}")
        return {"status": "ok", "enabled": body.enabled}

    @router.post("/{automation_id}/run", response_model=AutomationRunView)
    async def run_now(automation_id: str, tenant_id: str | None = Query(None)) -> AutomationRunView:
        """Run an automation immediately — the "try it" button.

        Goes through the same runner as a real trigger, so it honours the kill switch, the
        rate cap, and the autonomy dial: a test run that behaved differently from a real
        one would be worse than no test run at all. The ledger records it with a ``manual``
        verdict so it is distinguishable afterwards.
        """
        automation = await store.get(
            tenant=tenant_id or default_tenant, automation_id=automation_id
        )
        if automation is None:
            raise HTTPException(status_code=404, detail=f"no such automation: {automation_id}")
        run = await runner.run_once(automation, trigger_refs=[], summaries=[], verdict="manual")
        if run is None:
            raise HTTPException(
                status_code=409,
                detail="automations are halted for this tenant (kill switch)",
            )
        return _run_view(run)

    @router.delete("/{automation_id}")
    async def delete_automation(
        automation_id: str, tenant_id: str | None = Query(None)
    ) -> Response:
        ok = await store.delete(tenant=tenant_id or default_tenant, automation_id=automation_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such automation: {automation_id}")
        return Response(status_code=204)

    return router


__all__ = [
    "AutomationRunView",
    "AutomationView",
    "CreateAutomationRequest",
    "KillSwitchBody",
    "TemplateView",
    "create_automations_router",
]
