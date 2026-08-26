"""Unit tests for the core's own ``review`` page — the governed approval path (ADR-0093 §2).

Covers the contract the shell renders (a ``ReviewSuggestion`` per #542/ADR-0090), the apply
paths through each store (ADR-0093 §3), the operator's editable draft, and the resolved-decision
trail the reflection pass reads back as negative context (ADR-0093 §6).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent.instructions import AgentInstructionsStore
from epicurus_core_app.agent.playbook_review import (
    CORE_MODULE_NAME,
    CORE_REVIEW_PAGE_ID,
    INSTRUCTIONS_PATH,
    CoreReviewPage,
    PlaybookProposalStore,
    _StoredProposal,
    playbook_path,
)
from epicurus_core_app.agent.playbooks import PlaybookStore

TENANT = "t1"


class _Harness:
    """The three stores plus the page, all over one in-memory engine."""

    def __init__(
        self,
        page: CoreReviewPage,
        store: PlaybookProposalStore,
        instructions: AgentInstructionsStore,
        playbooks: PlaybookStore,
        engine: AsyncEngine,
    ) -> None:
        self.page = page
        self.store = store
        self.instructions = instructions
        self.playbooks = playbooks
        self.engine = engine

    async def seed_legacy_instructions_proposal(
        self, content: str, *, tenant: str = TENANT, note: str = ""
    ) -> str:
        """Insert a pre-#762 pending row against the base instructions, bypassing ``add()``.

        ``add()`` refuses the instructions path now, but a deployment upgrading with such a
        row already staged must still render it and let the operator Reject it — this is how
        the tests fabricate that upgrade-time state. Returns the row's ``sid``.
        """
        sid = uuid.uuid4().hex
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            session.add(
                _StoredProposal(
                    sid=sid,
                    tenant=tenant,
                    path=INSTRUCTIONS_PATH,
                    operation="update",
                    proposed_content=content,
                    origin="reflection",
                    note=note,
                )
            )
            await session.commit()
        return sid


def _engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _fresh() -> _Harness:
    engine = _engine()
    playbooks = PlaybookStore(engine)
    instructions = AgentInstructionsStore(engine, default="BASE", playbooks=playbooks)
    store = PlaybookProposalStore(engine)
    for s in (playbooks, instructions, store):
        await s.init()
    page = CoreReviewPage(
        store=store,
        instructions=instructions,
        playbooks=playbooks,
        tenant=TENANT,
        version="9.9.9",
    )
    return _Harness(page, store, instructions, playbooks, engine)


# ── the manifest / pseudo-module identity ────────────────────────────────────


async def test_manifest_declares_one_review_page_and_no_tool_surface() -> None:
    """The core contributes a page, never tools: it is not a module (ADR-0093 §2)."""
    h = await _fresh()
    manifest = h.page.manifest()

    assert manifest.name == CORE_MODULE_NAME
    assert [(p.id, p.archetype) for p in manifest.pages] == [(CORE_REVIEW_PAGE_ID, "review")]
    assert manifest.tools == []
    assert manifest.reindexable is False
    assert manifest.resolver is False
    assert manifest.attachable is False


async def test_unknown_page_id_404s() -> None:
    h = await _fresh()
    with pytest.raises(HTTPException) as exc:
        await h.page.get_page("nope")
    assert exc.value.status_code == 404


# ── the pending queue + diff shape (the #542 contract) ───────────────────────


async def test_empty_queue_renders_an_empty_review() -> None:
    h = await _fresh()
    data = await h.page.list_review()
    assert data.suggestions == []


async def test_a_legacy_instructions_proposal_still_renders_with_its_diff() -> None:
    """A pre-#762 pending row against the base prompt must stay visible and reviewable —
    the operator has to be able to see what they're rejecting (#762 keeps the read path)."""
    h = await _fresh()
    await h.instructions.set_instructions(TENANT, "Be terse.")
    await h.seed_legacy_instructions_proposal(
        "Be terse and cite sources.", note="You corrected this twice."
    )

    [s] = (await h.page.list_review()).suggestions
    assert s.operation == "update"
    assert s.title == "Base instructions"
    assert s.current == "Be terse."  # the live document
    assert s.content == "Be terse and cite sources."  # the proposal
    assert s.note == "You corrected this twice."
    assert "+Be terse and cite sources." in s.diff
    assert "-Be terse." in s.diff


async def test_a_legacy_instructions_proposal_diffs_against_the_default_when_unset() -> None:
    """An untouched tenant diffs against the shipped default, not against ""."""
    h = await _fresh()
    await h.seed_legacy_instructions_proposal("NEW")
    [s] = (await h.page.list_review()).suggestions
    assert s.current == "BASE"


async def test_a_legacy_instructions_proposal_never_diffs_against_base_plus_playbooks() -> None:
    """The diff target is the editable document, not the composed prompt (ADR-0093 §4)."""
    h = await _fresh()
    await h.instructions.set_instructions(TENANT, "Be terse.")
    await h.playbooks.create(TENANT, name="Briefing", content="Calendar before mail.")
    await h.seed_legacy_instructions_proposal("NEW")

    [s] = (await h.page.list_review()).suggestions
    assert s.current == "Be terse."
    assert "Playbook" not in s.current


async def test_create_of_a_new_playbook_has_empty_current() -> None:
    h = await _fresh()
    await h.store.add(
        tenant=TENANT,
        path=playbook_path("Briefing"),
        operation="create",
        proposed_content="Calendar before mail.",
    )

    [s] = (await h.page.list_review()).suggestions
    assert s.operation == "create"
    assert s.title == "Briefing"
    assert s.current == ""  # nothing exists yet
    assert s.content == "Calendar before mail."


async def test_update_of_an_existing_playbook_diffs_against_it() -> None:
    h = await _fresh()
    await h.playbooks.create(TENANT, name="Briefing", content="old guidance")
    await h.store.add(
        tenant=TENANT,
        path=playbook_path("Briefing"),
        operation="update",
        proposed_content="new guidance",
    )

    [s] = (await h.page.list_review()).suggestions
    assert s.current == "old guidance"
    assert "+new guidance" in s.diff


async def test_queue_is_tenant_scoped() -> None:
    h = await _fresh()
    await h.store.add(
        tenant="other", path=playbook_path("P"), operation="create", proposed_content="x"
    )
    assert (await h.page.list_review()).suggestions == []


async def test_add_rejects_an_unsupported_operation() -> None:
    """The core proposes only update/create — never a delete (ADR-0093 §2)."""
    h = await _fresh()
    with pytest.raises(ValueError):
        await h.store.add(
            tenant=TENANT, path=playbook_path("P"), operation="delete", proposed_content=""
        )


async def test_add_refuses_the_instructions_path_for_every_origin() -> None:
    """#762's defense in depth: the sink itself refuses the base-instructions target, so no
    proposal source — reflection, or anything future — can stage one, whatever its origin."""
    h = await _fresh()
    for origin in ("reflection", "operator", "some-future-source"):
        with pytest.raises(ValueError, match="base instructions"):
            await h.store.add(
                tenant=TENANT,
                path=INSTRUCTIONS_PATH,
                operation="update",
                proposed_content="planted",
                origin=origin,
            )
    assert (await h.page.list_review()).suggestions == []


# ── approve: the apply paths (ADR-0093 §3) ───────────────────────────────────


async def test_approve_of_a_legacy_instructions_proposal_is_refused() -> None:
    """#762: the instructions apply-path is gone. A pre-existing pending proposal renders and
    can be rejected, but Approve refuses with a clear message and writes nothing."""
    h = await _fresh()
    sid = await h.seed_legacy_instructions_proposal("Be terse.")

    with pytest.raises(HTTPException) as exc:
        await h.page.approve(sid)
    assert exc.value.status_code == 409
    assert "no longer be approved" in str(exc.value.detail)
    assert await h.instructions.get_base(TENANT) == "BASE"  # untouched
    assert len((await h.page.list_review()).suggestions) == 1  # still pending, rejectable
    assert await h.store.decisions(tenant=TENANT) == []  # a refusal is not a decision


async def test_reject_of_a_legacy_instructions_proposal_still_resolves_it() -> None:
    """The queue must stay drainable across the upgrade: Reject records and removes the row."""
    h = await _fresh()
    sid = await h.seed_legacy_instructions_proposal("planted edit")

    result = await h.page.reject(sid)
    assert result.status == "rejected"
    assert (await h.page.list_review()).suggestions == []
    [d] = await h.store.decisions(tenant=TENANT)
    assert d.decision == "rejected"
    assert d.proposed_content == "planted edit"


async def test_approve_create_makes_the_playbook() -> None:
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT,
        path=playbook_path("Briefing"),
        operation="create",
        proposed_content="Calendar before mail.",
    )
    await h.page.approve(p.sid)

    made = await h.playbooks.get_by_name(TENANT, "Briefing")
    assert made is not None and made.content == "Calendar before mail."
    assert made.enabled is True


async def test_approve_update_saves_the_playbook() -> None:
    h = await _fresh()
    await h.playbooks.create(TENANT, name="Briefing", content="old")
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("Briefing"), operation="update", proposed_content="new"
    )
    await h.page.approve(p.sid)

    got = await h.playbooks.get_by_name(TENANT, "Briefing")
    assert got is not None and got.content == "new"


async def test_approve_applies_the_operators_edited_content_not_the_proposal() -> None:
    """ADR-0090: what gets applied is what the operator actually saw and okayed."""
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT,
        path=playbook_path("Briefing"),
        operation="create",
        proposed_content="agent's wording",
    )

    await h.page.approve(p.sid, "operator's wording")
    made = await h.playbooks.get_by_name(TENANT, "Briefing")
    assert made is not None and made.content == "operator's wording"


async def test_approve_create_of_an_already_existing_playbook_updates_it() -> None:
    """A playbook created between staging and approval turns the create into an update."""
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("Briefing"), operation="create", proposed_content="new"
    )
    await h.playbooks.create(TENANT, name="Briefing", content="raced in by hand")

    await h.page.approve(p.sid)
    got = await h.playbooks.get_by_name(TENANT, "Briefing")
    assert got is not None and got.content == "new"
    assert len(await h.playbooks.list_playbooks(TENANT)) == 1  # no duplicate


async def test_approve_update_of_a_vanished_playbook_409s() -> None:
    h = await _fresh()
    made = await h.playbooks.create(TENANT, name="Briefing", content="old")
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("Briefing"), operation="update", proposed_content="new"
    )
    await h.playbooks.delete(TENANT, made.id)

    with pytest.raises(HTTPException) as exc:
        await h.page.approve(p.sid)
    assert exc.value.status_code == 409


async def test_approve_unknown_suggestion_404s() -> None:
    h = await _fresh()
    with pytest.raises(HTTPException) as exc:
        await h.page.approve("nope")
    assert exc.value.status_code == 404


async def test_approve_of_an_unknown_target_path_400s() -> None:
    h = await _fresh()
    p = await h.store.add(tenant=TENANT, path="wat", operation="update", proposed_content="x")
    with pytest.raises(HTTPException) as exc:
        await h.page.approve(p.sid)
    assert exc.value.status_code == 400


# ── reject + the audit trail (ADR-0090 / ADR-0093 §6) ────────────────────────


async def test_reject_discards_without_applying() -> None:
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("Briefing"), operation="create", proposed_content="NOPE"
    )

    result = await h.page.reject(p.sid)
    assert result.status == "rejected"
    assert await h.playbooks.get_by_name(TENANT, "Briefing") is None  # nothing written
    assert (await h.page.list_review()).suggestions == []


async def test_reject_unknown_suggestion_404s() -> None:
    h = await _fresh()
    with pytest.raises(HTTPException) as exc:
        await h.page.reject("nope")
    assert exc.value.status_code == 404


async def test_approve_records_what_was_proposed_and_what_was_applied() -> None:
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("P"), operation="create", proposed_content="proposed"
    )
    await h.page.approve(p.sid, "edited")

    [d] = await h.store.decisions(tenant=TENANT)
    assert d.decision == "approved"
    assert d.proposed_content == "proposed"
    assert d.applied_content == "edited"  # the operator's delta is the point of the trail


async def test_reject_records_a_rejection_with_no_applied_content() -> None:
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("Briefing"), operation="create", proposed_content="x"
    )
    await h.page.reject(p.sid)

    [d] = await h.store.decisions(tenant=TENANT)
    assert d.decision == "rejected"
    assert d.proposed_content == "x"
    assert d.applied_content == ""


async def test_decisions_can_be_filtered_to_rejections() -> None:
    """The reflection pass digests rejections only (ADR-0093 §6) — approvals aren't negative."""
    h = await _fresh()
    ok = await h.store.add(
        tenant=TENANT, path=playbook_path("Yes flow"), operation="create", proposed_content="yes"
    )
    no = await h.store.add(
        tenant=TENANT, path=playbook_path("P"), operation="create", proposed_content="no"
    )
    await h.page.approve(ok.sid)
    await h.page.reject(no.sid)

    rejected = await h.store.decisions(tenant=TENANT, decision="rejected")
    assert [d.proposed_content for d in rejected] == ["no"]
    assert len(await h.store.decisions(tenant=TENANT)) == 2


async def test_audit_is_newest_first_and_tenant_scoped() -> None:
    h = await _fresh()
    for i in range(3):
        p = await h.store.add(
            tenant=TENANT, path=playbook_path(f"P{i}"), operation="create", proposed_content=f"c{i}"
        )
        await h.page.reject(p.sid)
    other = await h.store.add(
        tenant="other", path=playbook_path("Theirs"), operation="create", proposed_content="theirs"
    )
    assert other.sid

    decisions = await h.store.decisions(tenant=TENANT)
    assert [d.proposed_content for d in decisions] == ["c2", "c1", "c0"]
    assert await h.store.decisions(tenant="other") == []


async def test_review_audit_page_serves_the_trail() -> None:
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("P"), operation="create", proposed_content="x"
    )
    await h.page.reject(p.sid)

    data = await h.page.review_audit(CORE_REVIEW_PAGE_ID)
    decisions = data["decisions"]
    assert isinstance(decisions, list)
    assert len(decisions) == 1


# ── the dispatch surface the registry calls ──────────────────────────────────


async def test_review_action_dispatches_approve_and_reject() -> None:
    h = await _fresh()
    a = await h.store.add(
        tenant=TENANT, path=playbook_path("A"), operation="create", proposed_content="A"
    )
    out = await h.page.review_action(CORE_REVIEW_PAGE_ID, a.sid, "approve", "edited")
    assert out["status"] == "approved"
    made = await h.playbooks.get_by_name(TENANT, "A")
    assert made is not None and made.content == "edited"

    b = await h.store.add(
        tenant=TENANT, path=playbook_path("P"), operation="create", proposed_content="B"
    )
    out = await h.page.review_action(CORE_REVIEW_PAGE_ID, b.sid, "reject")
    assert out["status"] == "rejected"


async def test_review_action_rejects_an_unknown_action() -> None:
    h = await _fresh()
    p = await h.store.add(
        tenant=TENANT, path=playbook_path("P"), operation="create", proposed_content="x"
    )
    with pytest.raises(HTTPException) as exc:
        await h.page.review_action(CORE_REVIEW_PAGE_ID, p.sid, "detonate")
    assert exc.value.status_code == 404


async def test_get_page_serves_the_review_archetype_shape() -> None:
    h = await _fresh()
    await h.store.add(
        tenant=TENANT, path=playbook_path("P"), operation="create", proposed_content="x"
    )
    data = await h.page.get_page(CORE_REVIEW_PAGE_ID)
    assert data["title"] == "Playbooks"
    suggestions = data["suggestions"]
    assert isinstance(suggestions, list)
    assert len(suggestions) == 1
