"""Notes module — its manifest (pages, attach surface, declared events).

Notes is **attach-only by design**: it registers **no MCP tools**, so the agent has
no automatic access to a note's content. A note reaches the agent only when the user
attaches it to a turn (the ``attachable`` surface in :mod:`epicurus_notes.attachments`).
This is the line between Notes (you author + manually attach) and Knowledge (your
vault, agent-retrievable).

The left-nav **Notes** page is the ``editor`` archetype — core-rendered; this module
supplies the document data (:mod:`epicurus_notes.pages`).
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, PageSpec, UiSection
from epicurus_notes.pages import NOTES_PAGE_ID

MODULE_NAME = "notes"

# Published after a note is saved and (best-effort) indexed. Tenant-scoped at runtime.
SAVED_SUBJECT = "notes.saved"


def build_module() -> EpicurusModule:
    """Build the Notes module — pages + attach surface, deliberately tool-free."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.2.0",
        description=(
            "Author Obsidian-style notes saved to a private RAG collection. Notes are"
            " attach-only — the agent reads one only when you attach it to a turn."
        ),
        pages=[
            PageSpec(
                id=NOTES_PAGE_ID,
                title="Notes",
                archetype="editor",
                icon="pencil",
                nav_order=40,
            )
        ],
        attachable=True,
        ui=UiSection(
            icon="pencil",
            summary=(
                "A place to write notes in the ε editor. Each note is saved and indexed"
                " into its own private collection; the agent can read a note only when"
                " you attach it to a message — never automatically."
            ),
            status_url="/status",
        ),
    )

    module.emits(SAVED_SUBJECT, "published after a note is saved and indexed")

    # No tools: Notes exposes no agent-callable surface (attach-only access, #134).
    return module
