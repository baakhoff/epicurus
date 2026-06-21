"""Knowledge module — MCP tool surface.

Registers the tools the agent can call:

* ``knowledge_search`` — embed a query and return the top-k matching chunks
  from the indexed vault **and** the bundled platform docs, merged by score.
* ``knowledge_reindex`` — trigger an incremental re-scan of all sources: the
  operator vault, the bundled platform docs, and each enabled module's docs.
* ``knowledge_propose_edit`` — stage a create/update/delete of a vault note for
  operator review (ADR-0033, #220). The agent has **no** direct vault-write tool;
  every agent-initiated change goes through the review queue and lands only on approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from epicurus_core import (
    EntityRef,
    EpicurusModule,
    ModelSlot,
    PageSpec,
    UiAction,
    UiSection,
    tool_envelope,
)
from epicurus_knowledge.indexer import KnowledgeIndexer, SearchHit
from epicurus_knowledge.module_docs import ModuleDocsIndexer
from epicurus_knowledge.pages import VAULT_PAGE_ID
from epicurus_knowledge.refs import (
    KNOWLEDGE_KIND,
    SOURCE_DOC,
    SOURCE_NOTE,
    doc_title,
    encode_ref,
    safe_relative,
)
from epicurus_knowledge.suggestions import (
    REVIEW_PAGE_ID,
    SuggestionStore,
    validate_operation,
)

MODULE_NAME = "knowledge"

INDEX_COMPLETE_SUBJECT = "knowledge.index.completed"

# Usage documentation served at GET /module-docs (#215).
_DOCS: list[dict[str, Any]] = [
    {
        "path": "usage.md",
        "content": """\
# Knowledge module — usage guide

The knowledge module gives the agent semantic search over two sources:

* **Your vault** — the Obsidian markdown notes in the configured vault path
  (``VAULT_PATH``, default ``/vault``).
* **Platform docs** — the epicurus platform documentation bundled in the
  knowledge image (``DOCS_PATH``, default ``/docs``).
* **Module docs** — usage guides contributed by each enabled module that
  declares a ``docs_url`` in its manifest (#215).

## Searching

Ask the agent any natural-language question. The agent calls
``knowledge_search`` automatically when it decides context from your notes
or the platform docs would help.  You can also trigger it explicitly:

> "Search my knowledge base for notes about project goals."

Results are returned as scored chunks with hover-card chips you can click
to open the source document.

## Indexing

Notes are indexed incrementally: only new or changed files are embedded on
each run. The index is refreshed at service startup and whenever you click
**Re-index** in the Modules UI or the agent calls ``knowledge_reindex``.

After adding notes to your vault or changing the vault path, trigger a
re-index to pick up the changes.

## Live sync (watched vault)

If your vault is an Obsidian-synced folder bind-mounted into the container, set
``VAULT_WATCH=true`` to have the service watch it and re-index automatically when
files change on disk — no manual re-index needed. In this mode the vault is
**externally owned**: the Knowledge editor page is read-only and Obsidian is the
sole author. Edit notes in Obsidian; they sync to disk and re-index here within a
few seconds.

## Changing the embedding model

Pick a model in the Modules UI under **knowledge** → **Embedding model**.  After
changing, click **Re-index** — vectors are model-specific and must be
regenerated.  The previous vectors remain searchable until the re-index
completes.
""",
    },
    {
        "path": "tools.md",
        "content": """\
# Knowledge module — agent tools

## knowledge_search

Search the knowledge base for content relevant to a query.

**Parameters**
- ``query`` (string) — natural-language question or search phrase.
- ``k`` (integer, default 5) — maximum number of chunks to return.

**Returns** the top-*k* chunks sorted by cosine similarity across the vault
and platform-docs collections, with one entity-reference chip per distinct
source document.

## knowledge_reindex

Incrementally re-index all sources: vault, platform docs, and module docs.

**Parameters** — none.

**Returns** ``{"indexed": N, "deleted": M, "unchanged": K}`` summed across
all sources.

## knowledge_propose_edit

Propose a create/update/delete of a vault note **for operator review** — the
agent's only way to change the knowledge base. The edit is staged, never applied
directly; the operator approves or rejects it under **Knowledge → Suggestions**.

**Parameters**
- ``path`` (string) — vault-relative ``.md`` path, e.g. ``projects/goals.md``.
- ``content`` (string) — full proposed content (required for create/update).
- ``operation`` (string) — ``create``, ``update``, or ``delete`` (default ``update``).
- ``note`` (string, optional) — short rationale shown beside the diff.

**Returns** a confirmation that the suggestion was queued. Nothing is written or
indexed until the operator approves it (ADR-0033).
""",
    },
]


def build_module(
    vault_indexer: KnowledgeIndexer,
    docs_indexer: KnowledgeIndexer,
    module_docs_indexer: ModuleDocsIndexer,
    suggestions: SuggestionStore,
    *,
    tenant: str,
    vault_path: Path,
) -> EpicurusModule:
    """Build the knowledge module and register its tools.

    Args:
        vault_indexer: Indexer for the operator's Obsidian vault
            (``<tenant>__knowledge`` collection).
        docs_indexer: Indexer for the bundled platform docs
            (``<tenant>__docs`` collection).
        module_docs_indexer: Indexer for per-module documentation (#215),
            also written to ``<tenant>__docs`` under ``module/<name>/`` prefixes.
        suggestions: Store for agent-proposed vault changes awaiting review (#220).
        tenant: The tenant whose suggestion queue the propose tool writes to.
        vault_path: Vault root, used to path-confine a proposed edit's target.
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.13.0",
        description=(
            "Obsidian vault RAG + platform self-documentation: semantic search"
            " and incremental indexing."
        ),
        pages=[
            PageSpec(
                id=VAULT_PAGE_ID,
                title="Knowledge",
                archetype="editor",
                icon="book",
                nav_order=30,
            ),
            # The review queue for agent-proposed changes (ADR-0033, #220).
            PageSpec(
                id=REVIEW_PAGE_ID,
                title="Suggestions",
                archetype="review",
                icon="inbox",
                nav_order=31,
            ),
        ],
        ui=UiSection(
            icon="book",
            summary=(
                "Indexes your Obsidian vault and the epicurus platform docs so the"
                " agent can answer questions grounded in your notes and the"
                " platform documentation."
            ),
            config_schema={
                "type": "object",
                "properties": {
                    "vault_path": {
                        "type": "string",
                        "title": "Vault path",
                        "description": "Absolute path inside the container to the Obsidian vault.",
                    }
                },
            },
            status_url="/status",
            actions=[
                UiAction(
                    tool="knowledge_reindex",
                    label="Re-index",
                    description="Incrementally re-index the vault, platform docs, and module docs.",
                )
            ],
        ),
        # Vault documents can be attached to a chat turn (#137) — picker + resolve below.
        attachable=True,
        # Cited documents resolve to a hover-card (#143) — see resolver.py.
        resolver=True,
        # The operator picks the embedding model on the Modules page (#128); the indexer
        # reads the choice via PlatformClient.get_module_model("embedding"), falling back
        # to the core default when unset. Changing it requires a re-index (vectors are
        # model-specific) — trigger "Re-index" after switching.
        required_models=[
            ModelSlot(
                key="embedding",
                role="embedding",
                label="Embedding model",
                description="Model used to embed vault notes and search queries.",
            )
        ],
        # Contribute usage docs for the knowledge module itself (#215).
        docs_url="/module-docs",
    )

    module.emits(INDEX_COMPLETE_SUBJECT, "published after each incremental index run")

    @module.tool()
    async def knowledge_search(query: str, k: int = 5) -> str:
        """Search the knowledge base for content relevant to *query*.

        Searches both the operator's Obsidian vault (``<tenant>__knowledge``) and the
        bundled platform docs (``<tenant>__docs``), merging and re-ranking by cosine
        similarity to return the top *k* chunks across both sources.

        Returns the matching chunks as readable text (so you can quote or reason over
        them) plus one entity-reference chip per cited document: hovering a chip shows a
        hover-card (path, tags, last-indexed) and clicking a vault note opens it in the
        Knowledge page. Platform-docs citations are shown with a ``docs/`` path prefix so
        you can tell them apart from vault notes.

        Args:
            query: Natural-language question or search phrase.
            k: Maximum number of chunks to return (default 5).
        """
        vault_hits, docs_hits = await _search_both(vault_indexer, docs_indexer, query, k)
        tagged = [(SOURCE_NOTE, hit) for hit in vault_hits]
        tagged += [(SOURCE_DOC, hit) for hit in docs_hits]
        tagged.sort(key=lambda pair: pair[1]["score"], reverse=True)
        top = tagged[:k]
        if not top:
            return tool_envelope("No matching content found.", [])

        lines = [f"Found {len(top)} relevant chunk(s):", ""]
        refs: list[EntityRef] = []
        seen: set[str] = set()
        for n, (source, hit) in enumerate(top, start=1):
            path = hit["note_path"]
            display = f"docs/{path}" if source == SOURCE_DOC else path
            heading = hit["heading"]
            lines.append(f"{n}. {display}" + (f" — {heading}" if heading else ""))
            lines.append(hit["text"])
            lines.append("")
            ref_id = encode_ref(source, path)
            if ref_id not in seen:  # one chip per distinct document, not per chunk
                seen.add(ref_id)
                refs.append(
                    EntityRef(
                        ref_id=ref_id,
                        module=MODULE_NAME,
                        kind=KNOWLEDGE_KIND,
                        title=heading or doc_title(path),
                        summary=_snippet(hit["text"]),
                    )
                )
        return tool_envelope("\n".join(lines).rstrip(), refs)

    @module.tool()
    async def knowledge_reindex() -> dict[str, int]:
        """Incrementally re-index all knowledge sources.

        Walks the Obsidian vault, the bundled platform docs, and each enabled
        module's declared docs, embedding new or changed files via the core's LLM
        gateway and removing vectors for deleted files.  Unchanged files are skipped.

        Returns ``{"indexed": N, "deleted": M, "unchanged": K}`` summed across
        all sources.
        """
        vault_result = await vault_indexer.run()
        docs_result = await docs_indexer.run()
        module_result = await module_docs_indexer.run()
        return {
            "indexed": (
                vault_result["indexed"] + docs_result["indexed"] + module_result["indexed"]
            ),
            "deleted": (
                vault_result["deleted"] + docs_result["deleted"] + module_result["deleted"]
            ),
            "unchanged": (
                vault_result["unchanged"] + docs_result["unchanged"] + module_result["unchanged"]
            ),
        }

    @module.tool()
    async def knowledge_propose_edit(
        path: str,
        content: str = "",
        operation: str = "update",
        note: str = "",
    ) -> str:
        """Propose a change to a vault note for the operator to review (ADR-0033, #220).

        This does **not** modify the vault. The change is staged as a suggestion the
        operator approves or rejects in the **Knowledge → Suggestions** page; only on
        approval is it written and indexed. Use this whenever you want to add, edit, or
        remove a note — it is your only path to changing the knowledge base.

        Args:
            path: Vault-relative path of the note, e.g. ``projects/goals.md``. Must end
                in ``.md`` and stay inside the vault (no ``..`` traversal).
            content: The note's full proposed content. Required for ``create``/``update``;
                ignored for ``delete``.
            operation: ``create`` (new note), ``update`` (replace an existing note's
                content), or ``delete`` (remove a note). Defaults to ``update``.
            note: An optional short rationale shown to the operator alongside the diff.

        Returns a confirmation that the suggestion was queued, or an error describing why
        the path or operation was rejected.
        """
        try:
            op = validate_operation(operation)
        except ValueError as exc:
            return tool_envelope(str(exc), [])
        try:
            # Path-confine to the vault (``.md`` only, no traversal) before staging it.
            safe_relative(vault_path, path)
        except Exception as exc:  # HTTPException(detail=...) from safe_relative
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"Cannot propose change to {path!r}: {detail}", [])
        proposed = "" if op == "delete" else content
        suggestion = await suggestions.add(
            tenant=tenant,
            path=path,
            operation=op,
            proposed_content=proposed,
            origin="agent",
            note=note,
        )
        return tool_envelope(
            f"Proposed {op} of '{path}' (suggestion {suggestion.sid[:8]}). It is pending"
            " your review in Knowledge → Suggestions; nothing changes until you approve it.",
            [],
        )

    return module


def module_docs() -> dict[str, Any]:
    """The knowledge module's own documentation pages for auto-indexing (#215)."""
    return {"documents": _DOCS}


def _snippet(text: str, limit: int = 120) -> str:
    """A short, single-line preview of a chunk for an entity-reference chip."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit].rstrip() + "…"


async def _search_both(
    vault_indexer: KnowledgeIndexer,
    docs_indexer: KnowledgeIndexer,
    query: str,
    k: int,
) -> tuple[list[SearchHit], list[SearchHit]]:
    """Run vault and docs searches concurrently and return both result lists."""
    import asyncio

    vault_task = asyncio.create_task(vault_indexer.search(query, k))
    docs_task = asyncio.create_task(docs_indexer.search(query, k))
    vault_hits, docs_hits = await asyncio.gather(vault_task, docs_task)
    return vault_hits, docs_hits
