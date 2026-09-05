"""Notes module — manifest + the agent's (write-only) tool surface.

Notes are **private**: the agent has **no read access** to a note's body. There is no
get/read tool, and the `.md` mirror is hidden from the storage module's file tools — a
note reaches the agent's context only when the user attaches it to a turn
(:mod:`epicurus_notes.attachments`). This is the line between Notes (private; you author +
manually attach) and Knowledge (your vault, agent-retrievable).

The agent *can* see what notes exist and propose changes (#KB-refactor):

* ``notes_list`` / ``notes_tree`` — titles + structure only (never bodies).
* ``notes_create`` / ``notes_propose_edit`` / ``notes_append`` / ``notes_delete`` — staged
  for operator review (ADR-0033), exactly like the knowledge base. Nothing is written until
  approved; ``append`` adds text the agent supplies (it can't read the note) onto the
  current body.

Suggestion lifecycle (#744) — the agent's own view of its pending review queue, so "change
that suggestion" revises it in place instead of piling up a near-duplicate:

* ``notes_list_suggestions`` — the pending queue (id, kind, slug, a content preview).
* ``notes_read_suggestion`` — one suggestion's full proposed content (its own draft — not
  a note body the agent otherwise can't read).
* ``notes_update_suggestion`` — revise a pending suggestion's content/note.
* ``notes_withdraw_suggestion`` — retract a pending suggestion (kept as history).

Approve/reject stay off the MCP surface (exposing them would let the agent approve its own
proposals); update and withdraw are safe to expose because both stay inside the pending
queue and never touch a note.

The left-nav **Notes** page is the ``editor`` archetype; the **Note suggestions** page is
the ``review`` archetype — both core-rendered (this module supplies data only).
"""

from __future__ import annotations

from epicurus_core import (
    AutomationTemplate,
    EpicurusModule,
    PageSpec,
    PlatformClient,
    UiSection,
    WritesDocument,
    event_subject,
    tool_envelope,
)
from epicurus_notes.db import NotesStore
from epicurus_notes.events import NOTE_CREATED, NOTE_DELETED, NOTE_UPDATED
from epicurus_notes.pages import NOTES_PAGE_ID
from epicurus_notes.suggestions import (
    REVIEW_PAGE_ID,
    NoteSuggestionReview,
    NoteSuggestionStore,
    validate_note_operation,
)

MODULE_NAME = "notes"

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
        version="0.13.0",
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
        # Starter presets for the Templates tab (#705, ADR-0105) — never auto-instantiated.
        automation_templates=[
            AutomationTemplate(
                key="weekly-notes-review",
                name="Weekly notes review",
                description=(
                    "A lightweight weekly recap of which notes you've been touching —"
                    " titles only, notes stay private."
                ),
                trigger={"cadence": "weekly", "hour": 17, "weekday": 4},
                prompt=(
                    "List your notes and call out which ones look recently touched, as a"
                    " short weekly recap. You cannot read note bodies — titles only."
                ),
                autonomy="notify",
                sinks=["push"],
            ),
            AutomationTemplate(
                key="on-note-created",
                name="Tell me when a note is created",
                description=(
                    "Runs whenever a note comes into existence (editor or approved suggestion)."
                ),
                trigger={"module": MODULE_NAME, "event_type": NOTE_CREATED},
                prompt="A note was created. Name it in one short line.",
                autonomy="notify",
                sinks=["push"],
            ),
        ],
    )

    # Spine emitters (#665) — replaces the legacy bare `notes.saved` subject, which had no
    # consumer (the same migration mail.sent made, #663). Updates are debounced to settled
    # saves; created/deleted fire immediately.
    module.emits(
        event_subject(NOTE_CREATED),
        "a note came into existence (editor or approved suggestion)",
    )
    module.emits(
        event_subject(NOTE_UPDATED),
        "a note's editing session settled — debounced, one event per quiet window, "
        "carrying the last save's timestamp",
    )
    module.emits(
        event_subject(NOTE_DELETED),
        "a note was deleted (editor or approved suggestion)",
    )

    # ── Read-only structure (titles only — never bodies; notes are private) ──────

    @module.tool(side_effect="read")
    async def notes_list() -> str:
        """List your notes — their titles and slugs, newest first. **Never returns bodies.**

        Notes are private: use this to discover what exists so you can propose a change to
        the right one. To read a note's content, the user must attach it to the message.
        """
        summaries = await store.list_summaries(tenant=tenant)
        if not summaries:
            return "No notes yet."
        return "Notes (title — slug):\n" + "\n".join(f"- {s.title} — {s.slug}" for s in summaries)

    @module.tool(side_effect="read")
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
        """Stage a suggestion, or auto-apply it when review is off for this module.

        Every caller is annotated ``side_effect="propose"`` (#721, ADR-0112): accurate when
        review is on for this module; with it off, a propose-autonomy automation inherits the
        same direct-apply behavior chat already has here — an accepted interaction, not a bug
        the annotation should paper over (see the ADR for why forcing automations to always
        stage isn't the resolution).
        """
        clean = _valid_slug(slug)
        if clean is None:
            # Raise (not a success envelope) so the call is structurally an error: the live
            # document pane keys `doc.failed` off the MCP call's `isError`, not the returned
            # text (#690) — a `tool_envelope` here would open the pane on a write that never
            # happened.
            raise ValueError(f"Invalid note slug: {slug!r}")
        # validate_note_operation already raises ValueError on an unknown operation; every
        # caller below passes a fixed literal, so this never fires today, but propagating it
        # (rather than wrapping it in a tool_envelope) keeps it consistent if that changes.
        op = validate_note_operation(operation)
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
            # Raise rather than return a success envelope (#690): the suggestion stays staged
            # (nothing is lost), but the direct-apply the caller asked for did not happen, so
            # the pane must not treat `doc.target` as written.
            raise RuntimeError(f"{pending} (review is off but applying failed: {detail})") from exc
        return tool_envelope(
            f"{verb.capitalize()} note '{clean}' applied directly — review is off.", []
        )

    # `content` is the note's whole body, so the shell can show it in the document pane as the
    # note being written (#541, ADR-0100). `notes_append` is deliberately NOT annotated (for
    # `writes_document` — it does carry `side_effect`, below): its `text` is a fragment the
    # server concatenates on approval, not a document.
    # side_effect="propose" (#721, ADR-0112): stages unless review is off for this module
    # (PlatformClient.get_suggestions_enabled()), in which case _stage applies it directly —
    # an accepted, documented interaction (docs/reference/automations.md), not a bug.
    @module.tool(
        writes_document=WritesDocument(content_arg="content", target_arg="slug"),
        side_effect="propose",
    )
    async def notes_create(slug: str, content: str, note: str = "") -> str:
        """Propose creating a note at *slug* with *content*, for operator review (ADR-0033).

        Staged as a suggestion — nothing is written until you approve it. *slug* is the
        note's id (e.g. ``meeting-2026-06-24`` or ``work/ideas``); the title is derived from
        the body. *note* is an optional rationale shown beside the diff.

        Check ``notes_list_suggestions`` first — if you already proposed this note and it's
        still pending, revise it with ``notes_update_suggestion`` instead of creating a
        near-duplicate.
        """
        return await _stage(slug, "create", content, note)

    # side_effect="propose" (#721, ADR-0112) — same review-toggle interaction as
    # notes_create above.
    @module.tool(
        writes_document=WritesDocument(content_arg="content", target_arg="slug"),
        side_effect="propose",
    )
    async def notes_propose_edit(slug: str, content: str, note: str = "") -> str:
        """Propose replacing note *slug*'s full body with *content*, for review (ADR-0033).

        Staged as a suggestion. Since notes are private you cannot read the current body —
        you propose the full new content and the operator reviews the diff. For purely
        additive changes prefer ``notes_append``.

        If the user asks you to change or fix a suggestion you already made, check
        ``notes_list_suggestions`` — if it's still pending, revise it with
        ``notes_update_suggestion`` rather than proposing this edit a second time.
        """
        return await _stage(slug, "update", content, note)

    # side_effect="propose" (#721, ADR-0112) — same review-toggle interaction as
    # notes_create above.
    @module.tool(side_effect="propose")
    async def notes_append(slug: str, text: str, note: str = "") -> str:
        """Propose appending *text* to the end of note *slug*, for review (ADR-0033).

        Staged as a suggestion. The server concatenates *text* onto the current body on
        approval (you supply only what to add — you cannot read the note).

        Check ``notes_list_suggestions`` first — if you already proposed an append here and
        it's still pending, revise it with ``notes_update_suggestion`` instead of appending
        it again on top.
        """
        return await _stage(slug, "append", text, note)

    # side_effect="propose" (#721, ADR-0112) — same review-toggle interaction as
    # notes_create above.
    @module.tool(side_effect="propose")
    async def notes_delete(slug: str, note: str = "") -> str:
        """Propose deleting note *slug*, for operator review (ADR-0033).

        Staged as a suggestion; the note is removed only on approval.

        Check ``notes_list_suggestions`` first — a delete already proposed for this slug
        needs no second one.
        """
        return await _stage(slug, "delete", "", note)

    # ── Suggestion lifecycle (#744): see and revise what's already pending ───────
    #
    # Before #744 the agent was write-only into the review queue — "change that suggestion"
    # could only create a second one. These let it see the pending queue and revise/withdraw
    # its own entries in place. Approve/reject stay off the MCP surface (letting the agent
    # approve its own proposals would defeat the review gate); update/withdraw don't have
    # that problem — both stay strictly within the pending queue and never touch a note.

    def _preview(content: str, limit: int = 80) -> str:
        """A short single-line preview of *content* for a suggestion-queue listing."""
        flat = " ".join(content.split())
        return flat if len(flat) <= limit else f"{flat[:limit].rstrip()}…"

    @module.tool(side_effect="read")
    async def notes_list_suggestions(status: str = "pending") -> str:
        """List your own suggestions awaiting operator review (#744).

        **Check this before proposing a change** — if something similar is already pending,
        revise it with ``notes_update_suggestion`` instead of creating a near-duplicate the
        operator's review queue would otherwise accumulate.

        Args:
            status: Must be ``"pending"`` — the only queryable state today. Approved,
                rejected, and withdrawn suggestions are immutable history, not listed here.
        """
        if status != "pending":
            raise ValueError(
                f"status must be 'pending', got {status!r} — approved/rejected/withdrawn"
                " suggestions are immutable history, not listed here"
            )
        pending = await suggestions.list(tenant=tenant)
        if not pending:
            return "No suggestions pending review."
        lines = ["Pending suggestions:"]
        for s in pending:
            bits = [f"- {s.sid} [{s.operation}] {s.slug}", f"proposed {s.created_at.isoformat()}"]
            preview = _preview(s.proposed_content) if s.proposed_content else ""
            if preview:
                bits.append(f'"{preview}"')
            if s.note:
                bits.append(f"note: {s.note}")
            lines.append(" — ".join(bits))
        return "\n".join(lines)

    @module.tool(side_effect="read")
    async def notes_read_suggestion(id: str) -> str:
        """Read one pending suggestion's full proposed content (#744).

        This is your own draft, not a note body you otherwise can't read — notes stay
        private; you're only seeing back what you proposed.

        Args:
            id: The suggestion id, from ``notes_list_suggestions``.
        """
        try:
            s = await review.read(id)
        except Exception as exc:
            raise ValueError(getattr(exc, "detail", str(exc))) from exc
        lines = [f"Suggestion {s.sid} — {s.operation} — {s.slug}"]
        lines.append(f"Proposed: {s.created_at.isoformat()}")
        if s.note:
            lines.append(f"Note: {s.note}")
        if s.proposed_content:
            lines.extend(["", s.proposed_content])
        return "\n".join(lines)

    @module.tool(side_effect="propose")
    async def notes_update_suggestion(
        id: str, content: str | None = None, note: str | None = None
    ) -> str:
        """Revise a PENDING suggestion in place, instead of proposing a near-duplicate (#744).

        Use this when you want to change a suggestion you already made, before the operator
        has reviewed it. It stays pending; the operator's review shows the latest revision
        as one queue entry, never two. Nothing touches the note — the review gate is
        unchanged. Pass only the fields you want to change; the rest keep their current
        value.

        Args:
            id: The suggestion id.
            content: New proposed content (the full body for create/update; the fragment
                to append for an append suggestion); omit to leave unchanged.
            note: New rationale shown to the operator; omit to leave unchanged.

        Refuses with an explanatory error if the suggestion was already approved, rejected,
        or withdrawn (immutable history), or does not exist.
        """
        try:
            updated = await review.update(id, content=content, note=note)
        except Exception as exc:
            raise ValueError(getattr(exc, "detail", str(exc))) from exc
        return (
            f"Suggestion {updated.sid} revised; still pending your review in"
            " Notes → Note suggestions."
        )

    @module.tool(side_effect="propose")
    async def notes_withdraw_suggestion(id: str) -> str:
        """Retract a PENDING suggestion you no longer want applied (#744).

        Use this when a suggestion you made has gone stale — e.g. superseded by a later
        edit, or the user changed their mind. Removes it from the pending queue (kept as
        history); nothing about the note is touched. Refuses with an explanatory error if
        it was already approved, rejected, or withdrawn, or does not exist.

        Args:
            id: The suggestion id.
        """
        try:
            result = await review.withdraw(id)
        except Exception as exc:
            raise ValueError(getattr(exc, "detail", str(exc))) from exc
        return f"Suggestion {result.id} withdrawn."

    return module
