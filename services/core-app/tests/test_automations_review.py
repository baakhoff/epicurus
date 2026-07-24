"""The automations review page + the ``propose_automation`` built-in (#667, ADR-0107).

Covers the hard guardrail (the tool stages only — it never creates or enables an automation), the
two-pipeline acceptance example, the pre-approval model picker, reject leaving nothing behind, and
the update path's readable diff. Plus the :class:`CorePages` composite that lets the reserved
``core`` name serve both this page and the playbooks page.

File-backed SQLite per test (``tmp_path``), not ``:memory:`` — the store convention across the
automations suite, and immune to the StaticPool cross-task rollback trap even though these tests
are single-task.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core.manifest import PageSpec, UiSection
from epicurus_core_app.automations.model import EventTrigger
from epicurus_core_app.automations.review import (
    CORE_AUTOMATIONS_PAGE_ID,
    PROPOSE_AUTOMATION_SPEC,
    AutomationProposalStore,
    CoreAutomationReviewPage,
    make_propose_automation_handler,
)
from epicurus_core_app.automations.store import AutomationStore
from epicurus_core_app.core_review import CorePages

TENANT = "local"

_Handler = Callable[[dict[str, Any], str], Awaitable[str]]


class _Env:
    def __init__(
        self,
        engine: AsyncEngine,
        automations: AutomationStore,
        proposals: AutomationProposalStore,
        page: CoreAutomationReviewPage,
        propose: _Handler,
    ) -> None:
        self.engine = engine
        self.automations = automations
        self.proposals = proposals
        self.page = page
        self.propose = propose  # the propose_automation built-in handler


async def _env(tmp_path: Path) -> _Env:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'review.db'}")
    automations = AutomationStore(engine)
    proposals = AutomationProposalStore(engine)
    await automations.init()
    await proposals.init()
    page = CoreAutomationReviewPage(store=proposals, automations=automations, tenant=TENANT)
    propose = make_propose_automation_handler(proposals, automations)
    return _Env(engine, automations, proposals, page, propose)


# The two pipelines from the acceptance example: "when I get mail, push me if it's important …;
# and each Monday 9AM write me a mail report."
_MAIL: dict[str, Any] = {
    "name": "Important mail alerts",
    "action": "Tell me when important mail arrives, and mark the rest read.",
    "autonomy": "notify",
    "sinks": ["push"],
    "event_trigger": {
        "module": "mail",
        "event_type": "mail.received",
        "matchers": [{"field": "importance", "op": "eq", "value": "high"}],
    },
}
_REPORT: dict[str, Any] = {
    "name": "Weekly mail report",
    "action": "Write me a summary of last week's mail.",
    "autonomy": "notify",
    "sinks": ["push"],
    "schedule_trigger": {"cadence": "weekly", "hour": 9, "weekday": 0},
}


# ── the hard guardrail ─────────────────────────────────────────────────────────


async def test_propose_stages_only_never_creates(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    msg = await env.propose(_MAIL, TENANT)
    assert "Staged" in msg
    # The guardrail: the tool created nothing — only a suggestion is staged.
    assert await env.automations.list(tenant=TENANT) == []
    assert len(await env.proposals.list_pending(tenant=TENANT)) == 1
    await env.engine.dispose()


async def test_guardrail_holds_across_many_calls(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    for _ in range(5):
        await env.propose(_MAIL, TENANT)
    # However many times it is called, at whatever the model intends, nothing is ever created —
    # the only path to an automation row is approve().
    assert await env.automations.list(tenant=TENANT) == []
    assert len(await env.proposals.list_pending(tenant=TENANT)) == 5
    await env.engine.dispose()


# ── the two-pipeline acceptance example ────────────────────────────────────────


async def test_two_pipelines_land_as_two_approvable_suggestions(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    await env.propose(_MAIL, TENANT)
    await env.propose(_REPORT, TENANT)
    data = await env.page.get_page(CORE_AUTOMATIONS_PAGE_ID)
    suggestions = data["suggestions"]
    assert len(suggestions) == 2
    assert all(s["operation"] == "create" for s in suggestions)
    assert all(s["automation"] is not None for s in suggestions)
    assert {s["automation"]["name"] for s in suggestions} == {
        "Important mail alerts",
        "Weekly mail report",
    }
    # Still nothing created — each is separately approvable.
    assert await env.automations.list(tenant=TENANT) == []
    await env.engine.dispose()


# ── approve: creates enabled, honours the model picker ─────────────────────────


async def test_approve_creates_enabled_and_respects_model_change(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    await env.propose(_MAIL, TENANT)  # drafted with no model
    (staged,) = await env.proposals.list_pending(tenant=TENANT)
    result = await env.page.approve(staged.sid, "llama-3.3")  # operator picked a model
    assert result.status == "approved"
    (automation,) = await env.automations.list(tenant=TENANT)
    assert automation.enabled is True  # approval is the consent
    assert automation.model == "llama-3.3"  # the picker was honoured
    assert automation.source == "agent"
    assert automation.name == "Important mail alerts"
    assert automation.event_trigger is not None
    assert automation.event_trigger.module == "mail"
    # Queue cleared, decision recorded.
    assert await env.proposals.list_pending(tenant=TENANT) == []
    audit = await env.proposals.decisions(tenant=TENANT)
    assert len(audit) == 1
    assert audit[0].decision == "approved"
    await env.engine.dispose()


async def test_approve_empty_model_means_operator_default(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    await env.propose({**_MAIL, "model": "drafted-model"}, TENANT)
    (staged,) = await env.proposals.list_pending(tenant=TENANT)
    await env.page.approve(staged.sid, "")  # the picker's "operator default" option
    (automation,) = await env.automations.list(tenant=TENANT)
    assert automation.model is None
    await env.engine.dispose()


async def test_quick_approve_keeps_drafted_model(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    await env.propose({**_MAIL, "model": "drafted-model"}, TENANT)
    (staged,) = await env.proposals.list_pending(tenant=TENANT)
    await env.page.approve(staged.sid, None)  # a list quick-approve, no edit
    (automation,) = await env.automations.list(tenant=TENANT)
    assert automation.model == "drafted-model"
    await env.engine.dispose()


# ── reject: nothing behind ─────────────────────────────────────────────────────


async def test_reject_leaves_nothing_behind(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    await env.propose(_MAIL, TENANT)
    (staged,) = await env.proposals.list_pending(tenant=TENANT)
    result = await env.page.reject(staged.sid)
    assert result.status == "rejected"
    assert await env.automations.list(tenant=TENANT) == []
    assert await env.proposals.list_pending(tenant=TENANT) == []
    audit = await env.proposals.decisions(tenant=TENANT)
    assert len(audit) == 1
    assert audit[0].decision == "rejected"  # feeds the reflection job's negative context (#615)
    await env.engine.dispose()


# ── update: readable diff, edits in place ──────────────────────────────────────


async def test_update_shows_readable_diff_and_edits_in_place(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    existing = await env.automations.create(
        tenant=TENANT,
        name="Old name",
        prompt="Old action",
        autonomy="notify",
        event_trigger=EventTrigger(module="mail", event_type="mail.received"),
        sinks=["push"],
    )
    update_args = {
        "name": "New name",
        "action": "New action",
        "autonomy": "notify",
        "sinks": ["push"],
        "operation": "update",
        "automation_id": existing.id,
        "event_trigger": {"module": "mail", "event_type": "mail.received"},
    }
    await env.propose(update_args, TENANT)
    data = await env.page.get_page(CORE_AUTOMATIONS_PAGE_ID)
    (suggestion,) = data["suggestions"]
    assert suggestion["operation"] == "update"
    # The diff reads as before→after over the human-rendered lines, not raw JSON.
    assert "Old name" in suggestion["diff"]
    assert "New name" in suggestion["diff"]

    (staged,) = await env.proposals.list_pending(tenant=TENANT)
    await env.page.approve(staged.sid, None)
    automations = await env.automations.list(tenant=TENANT)
    assert len(automations) == 1  # edited in place, never duplicated
    assert automations[0].name == "New name"
    assert automations[0].prompt == "New action"
    assert automations[0].enabled is True  # the row's enabled state is preserved
    await env.engine.dispose()


async def test_update_to_missing_automation_is_an_error(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    msg = await env.propose(
        {**_MAIL, "operation": "update", "automation_id": "does-not-exist"}, TENANT
    )
    assert msg.startswith("error:")
    assert await env.proposals.list_pending(tenant=TENANT) == []
    await env.engine.dispose()


# ── invalid drafts are errors to the model, not crashes or stages ──────────────


async def test_two_triggers_is_an_error(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    bad = {**_MAIL, "schedule_trigger": {"cadence": "daily", "hour": 9}}
    msg = await env.propose(bad, TENANT)
    assert msg.startswith("error:")
    assert await env.proposals.list_pending(tenant=TENANT) == []
    await env.engine.dispose()


async def test_blank_action_is_an_error(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    msg = await env.propose({**_MAIL, "action": "   "}, TENANT)
    assert msg.startswith("error:")
    assert await env.proposals.list_pending(tenant=TENANT) == []
    await env.engine.dispose()


# ── the preview renders understandably ─────────────────────────────────────────


async def test_preview_renders_trigger_and_filter_in_words(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    await env.propose(_MAIL, TENANT)
    review = await env.page.list_review()
    (suggestion,) = review.suggestions
    assert suggestion.automation is not None
    preview = suggestion.automation
    assert preview.trigger == "When mail emits mail.received"
    assert "importance" in preview.filter
    assert preview.autonomy == "notify"
    assert preview.autonomy_label.startswith("Notify")
    assert preview.sinks == ["push"]
    await env.engine.dispose()


async def test_schedule_trigger_renders_weekly(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    await env.propose(_REPORT, TENANT)
    review = await env.page.list_review()
    (suggestion,) = review.suggestions
    assert suggestion.automation is not None
    assert suggestion.automation.trigger == "Every Monday at 09:00"
    await env.engine.dispose()


async def test_get_page_rejects_unknown_page(tmp_path: Path) -> None:
    env = await _env(tmp_path)
    with pytest.raises(HTTPException) as excinfo:
        await env.page.get_page("nope")
    assert excinfo.value.status_code == 404
    assert env.page.page_spec().id == CORE_AUTOMATIONS_PAGE_ID
    await env.engine.dispose()


def test_tool_spec_shape() -> None:
    fn = PROPOSE_AUTOMATION_SPEC["function"]
    assert fn["name"] == "propose_automation"
    assert set(fn["parameters"]["required"]) == {"name", "action", "autonomy"}


# ── the CorePages composite (ADR-0107) ─────────────────────────────────────────


class _FakeSubPage:
    """A minimal in-process review page, to test the composite's routing in isolation."""

    def __init__(self, page_id: str) -> None:
        self._id = page_id
        self.calls: list[tuple[object, ...]] = []

    def page_spec(self) -> PageSpec:
        return PageSpec(id=self._id, title=self._id.title(), archetype="review")

    async def get_page(self, page_id: str) -> dict[str, object]:
        self.calls.append(("get_page", page_id))
        return {"title": self._id, "suggestions": [{"id": self._id}]}

    async def review_action(
        self, page_id: str, suggestion_id: str, action: str, content: str | None = None
    ) -> dict[str, object]:
        self.calls.append(("review_action", page_id, suggestion_id, action, content))
        return {"id": suggestion_id, "status": action}

    async def review_audit(self, page_id: str, *, limit: int = 50) -> dict[str, object]:
        self.calls.append(("review_audit", page_id, limit))
        return {"decisions": []}


def _composite(*pages: _FakeSubPage) -> CorePages:
    return CorePages(
        name="core",
        version="9.9.9",
        description="playbooks and automations",
        ui=UiSection(icon="book-open", summary="s"),
        pages=list(pages),
    )


def test_composite_manifest_declares_every_page() -> None:
    composite = _composite(_FakeSubPage("playbooks"), _FakeSubPage("automations"))
    manifest = composite.manifest()
    assert manifest.name == "core"
    assert {p.id for p in manifest.pages} == {"playbooks", "automations"}
    assert all(p.archetype == "review" for p in manifest.pages)


async def test_composite_dispatches_by_page_id() -> None:
    playbooks = _FakeSubPage("playbooks")
    automations = _FakeSubPage("automations")
    composite = _composite(playbooks, automations)

    await composite.get_page("automations")
    assert automations.calls == [("get_page", "automations")]
    assert playbooks.calls == []  # the other page is untouched

    await composite.review_action("playbooks", "sid7", "approve", "edited")
    assert playbooks.calls[-1] == ("review_action", "playbooks", "sid7", "approve", "edited")

    await composite.review_audit("automations", limit=5)
    assert automations.calls[-1] == ("review_audit", "automations", 5)


async def test_composite_unknown_page_is_404() -> None:
    composite = _composite(_FakeSubPage("playbooks"))
    with pytest.raises(HTTPException) as excinfo:
        await composite.get_page("ghost")
    assert excinfo.value.status_code == 404


def test_composite_rejects_duplicate_page_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _composite(_FakeSubPage("dup"), _FakeSubPage("dup"))
