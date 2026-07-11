"""Tests for the shared `review`-archetype contract (ADR-0033, ADR-0090)."""

from __future__ import annotations

from epicurus_core.review import (
    ApplyResult,
    ApproveBody,
    ReviewAuditData,
    ReviewData,
    ReviewDecision,
    ReviewSuggestion,
)


def test_review_suggestion_defaults() -> None:
    s = ReviewSuggestion(
        id="s1",
        title="doc",
        path="a.md",
        operation="update",
        origin="agent",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert s.note == ""
    assert s.diff == ""
    assert s.to_path == ""
    assert s.current == ""
    assert s.content == ""


def test_review_data_defaults_to_empty_queue() -> None:
    data = ReviewData()
    assert data.title == "Suggestions"
    assert data.suggestions == []


def test_apply_result_defaults() -> None:
    result = ApplyResult(id="s1", status="approved", path="a.md", operation="update")
    assert result.indexed is False


def test_approve_body_defaults_to_no_edit() -> None:
    assert ApproveBody().content is None
    assert ApproveBody(content="edited").content == "edited"


def test_review_decision_round_trips_proposed_and_applied_content() -> None:
    # The crux of the audit contract (#542): proposed and applied can legitimately differ
    # when the operator edits the draft before approving.
    decision = ReviewDecision(
        id="s1",
        title="doc",
        path="a.md",
        operation="update",
        origin="agent",
        created_at="2026-01-01T00:00:00+00:00",
        decided_at="2026-01-01T00:05:00+00:00",
        decision="approved",
        proposed_content="agent proposal",
        applied_content="operator edit",
    )
    assert decision.proposed_content != decision.applied_content
    assert decision.decision == "approved"


def test_review_audit_data_defaults_to_empty() -> None:
    assert ReviewAuditData().decisions == []
