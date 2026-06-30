"""Notes module — manifest + the agent's (write-only) tool surface.

Notes are **private**: the agent has **no read access** to a note's body. There is no
get/read tool, and the `.md` mirror is hidden from the storage module's file tools — a
note reaches the agent's context only when the user attaches it to a turn
(:mod:`epicurus_notes.attachments`). This is the line between Notes (private; you author +
manually attach) and Knowledge (your vault, agent-retrievable).

The agent *can* see what notes exist and propose changes (#KB-refactor):

* ``notes_list`` / ``notes_tree`` — titles + structure only (never bodies).
* ``notes_propose_create`` / ``_edit`` / ``_append`` / ``_delete`` — staged for operator
  review (ADR-0033), exactly like the knowledge base. Nothing is written until approved;
  ``append`` adds text the agent supplies (it can't read the note) onto the current body.

The left-nav **Notes** page is the ``editor`` archetype; the **Note suggestions** page is
the ``review`` archetype — both core-rendered (this module supplies data only).
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, PageSpec, PlatformClient, UiSection, tool_envelope
from epicurus_notes.db import NotesStore
from epicurus_notes.pages import NOTES_PAGE_ID
from epicurus_notes.suggestions import (
    REVIEW_PAGE_ID,
    NoteSuggestionReview,
    NoteSuggestionStore,
    validate_note_operation,
)

MODULE_NAME = "notes"

# Published after a note is saved and (best-effort) indexed. Tenant-scoped at runtime.
SAVED_SUBJECT = "notes.saved"

_MAX_SLUG = 512


def _valid_slug(slug: str) -> str | None:
    """Return a cleaned slug, or ``None`` if it is unusable (empty / too long / control chars)."""
    s = slug.strip()
    if not s or len(s) > _MAX_SLUG or any(ord(ch) < 0x20 for ch in s):
        return None
    return s


def build_module(
    store: NotesStore,
    suggestions: NoteSuggestionStore,
    review: NoteSuggestionReview,
    platform: PlatformClient,
    *,
    tenant: str,
) -> EpicurusModule:
    """Build the Notes module: the editor + review pages, the attach surface, and the
    agent's write-only tool surface (no read — notes are private).

    *review* + *platform* let the propose tools auto-apply a change when the operator has
    turned review off for notes (#KB-refactor)."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.6.0",
        description=(
            "Author Obsidian-style notes saved to a private collection and mirrored as .md"
            " in the shared file space. Private: the agent never reads a note's body — it"
            " can list titles and propose changes (staged for your review), or read one only"
            " when you attach it to a turn."
        ),
        pages=[
            PageSpec(
                id=NOTES_PAGE_ID,
                title="Notes",
                archetype="editor",
                icon="pencil",
                nav_order=40,
            ),
            # The review queue for agent-proposed note changes (ADR-0033).
            PageSpec(
                id=REVIEW_PAGE_ID,
                title="Note suggestions",
                archetype="review",
                icon="inbox",
                nav_order=41,
            ),
        ],
        attachable=True,
        # Notes are embedded into <tenant>__notes: re-embed on demand when the embedding model
        # changes, via POST /reindex (the core's re-embed fan-out, #332).
        reindexable=True,
        ui=UiSection(
            icon="pencil",
            summary=(
                "A place to write notes in the ε editor. Each note is saved and indexed into"
                " its own private collection; the agent can list titles and propose changes"
                " for your review, but reads a note's content only when you attach it."
            ),
            status_url="/status",
        ),
    )

    module.emits(SAVED_SUBJECT, "published after a note is saved and indexed")

    # ── Read-only structure (titles only — never bodies; notes are private) ──────

    @module.tool()
    async def notes_list() -> str:
        """List your notes — their titles and slugs, newest first. **Never returns bodies.**

        Notes are private: use this to discover what exists so you can propose a change to
        the right one. To read a note's content, the user must attach it to the message.
        """
        summaries = await store.list_summaries(tenant=tenant)
        if not summaries:
            return "No notes yet."
        return "Notes (title — slug):\n" + "\n".join(f"- {s.title} — {s.slug}" for s in summaries)

    @module.tool()
    async def notes_tree() -> str:
        """Show your notes as a structure (folders inferred from ``/`` in slugs). Titles only.

        Like ``notes_list`` but grouped/indented by slug path — never returns bodies.
        """
        summaries = await store.list_summaries(tenant=tenant)
        if not summaries:
            return "No notes yet."
        lines: list[str] = []
        for s in sorted(summaries, key=lambda x: x.slug):
            depth = s.slug.count("/")
            leaf = s.slug.split("/")[-1]
            lines.append(f"{'  ' * depth}{leaf}  — {s.title}")
        return "\n".join(lines)

    # ── Writes — all staged for operator review (notes are private) ──────────────

    async def _stage(slug: str, operation: str, proposed: str, note: str) -> str:
        clean = _valid_slug(slug)
        if clean is None:
            return tool_envelope(f"Invalid note slug: {slug!r}", [])
        try:
            op = validate_note_operation(operation)
        except ValueError as exc:
            return tool_envelope(str(exc), [])
        s = await suggestions.add(
            tenant=tenant,
            slug=clean,
            operation=op,
            proposed_content=proposed,
            origin="agent",
            note=note,
        )
        verb = {"create": "create", "update": "edit", "append": "append to", "delete": "delete"}[op]
        pending = (
            f"Proposed to {verb} note '{clean}' (suggestion {s.sid[:8]}). It is pending your"
            " review in Notes → Note suggestions; nothing changes until you approve it."
        )
        # When the operator has turned review off, apply the change directly (#KB-refactor).
        try:
            review_on = await platform.get_suggestions_enabled()
        except Exception:
            review_on = True  # if the setting can't be read, default to the safe (review) path
        if review_on:
            return tool_envelope(pending, [])
        try:
            await review.approve(s.sid)
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"{pending} (review is off but applying failed: {detail})", [])
        return tool_envelope(
            f"{verb.capitalize()} note '{clean}' applied directly — review is off.", []
        )

    @module.tool()
    async def notes_create(slug: str, content: str, note: str = "") -> str:
        """Propose creating a note at *slug* with *content*, for operator review (ADR-0033).

        Staged as a suggestion — nothing is written until you approve it. *slug* is the
        note's id (e.g. ``meeting-2026-06-24`` or ``work/ideas``); the title is derived from
        the body. *note* is an optional rationale shown beside the diff.
        """
        return await _stage(slug, "create", content, note)

    @module.tool()
    async def notes_propose_edit(slug: str, content: str, note: str = "") -> str:
        """Propose replacing note *slug*'s full body with *content*, for review (ADR-0033).

        Staged as a suggestion. Since notes are private you cannot read the current body —
        you propose the full new content and the operator reviews the diff. For purely
        additive changes prefer ``notes_append``.
        """
        return await _stage(slug, "update", content, note)

    @module.tool()
    async def notes_append(slug: str, text: str, note: str = "") -> str:
        """Propose appending *text* to the end of note *slug*, for review (ADR-0033).

        Staged as a suggestion. The server concatenates *text* onto the current body on
        approval (you supply only what to add — you cannot read the note).
        """
        return await _stage(slug, "append", text, note)

    @module.tool()
    async def notes_delete(slug: str, note: str = "") -> str:
        """Propose deleting note *slug*, for operator review (ADR-0033).

        Staged as a suggestion; the note is removed only on approval.
        """
        return await _stage(slug, "delete", "", note)

    return module
