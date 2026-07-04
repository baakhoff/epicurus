"""Knowledge module — MCP tool surface.

Registers the tools the agent can call. The knowledge base is organised into **projects**
(top-level folders, each a "knowledge base"); documents are addressed ``<project>/<path>.md``.

Read-only navigation (so the agent knows where things live):

* ``knowledge_search`` — embed a query, return the top-k chunks from the vault **and** the
  bundled platform docs, merged by score.
* ``knowledge_list_projects`` — list the knowledge bases (projects).
* ``knowledge_tree`` — the folder/document structure of one or all knowledge bases.
* ``knowledge_read_document`` — read one document's content by path.

Writes — **every** agent change is staged for operator review (ADR-0033, #220); the agent
has no direct vault-write tool:

* ``knowledge_create_document`` — create a new document (single-purpose create tool).
* ``knowledge_propose_edit`` — update/delete an existing document (also accepts create).
* ``knowledge_propose_move`` — move/rename a document or folder.
* ``knowledge_propose_rename`` — rename in place (keeps the folder).
* ``knowledge_propose_folder`` — create a folder.
* ``knowledge_propose_project`` — create a new knowledge base.
* ``knowledge_reindex`` — re-scan all sources (vault projects, platform docs, module docs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from epicurus_core import (
    EntityRef,
    EpicurusModule,
    ModelSlot,
    PageSpec,
    PlatformClient,
    UiAction,
    UiSection,
    tool_envelope,
)
from epicurus_knowledge.indexer import KnowledgeIndexer, SearchHit
from epicurus_knowledge.module_docs import ModuleDocsIndexer
from epicurus_knowledge.pages import VAULT_PAGE_ID
from epicurus_knowledge.reader import VaultReader
from epicurus_knowledge.refs import (
    KNOWLEDGE_KIND,
    SOURCE_DOC,
    SOURCE_NOTE,
    doc_title,
    encode_ref,
    safe_dir_relative,
    safe_project,
    safe_vault_rel,
)
from epicurus_knowledge.suggestions import (
    REVIEW_PAGE_ID,
    SuggestionReview,
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

## knowledge_create_document

Create a **new** document — the single-purpose tool for adding a note (reach for this
whenever you want to write a new document, rather than the multi-operation
``knowledge_propose_edit``). Staged for operator review like every change; written and
indexed on approval, or applied immediately when review is off.

**Parameters**
- ``path`` (string) — vault-relative ``.md`` path of the new note, e.g.
  ``projects/goals.md``. Must not already exist.
- ``content`` (string) — full markdown content of the new document.
- ``note`` (string, optional) — short rationale shown beside the diff.

**Returns** a confirmation that the document was created (or queued for review), or an
error if the path is invalid or already exists.

## knowledge_propose_edit

Propose an **update or delete** of an existing vault note **for operator review** (to
*create* a note, prefer ``knowledge_create_document``). The edit is staged, never applied
directly; the operator approves or rejects it under **Knowledge → Suggestions**.

**Parameters**
- ``path`` (string) — vault-relative ``.md`` path, e.g. ``projects/goals.md``.
- ``content`` (string) — full proposed content (required for create/update).
- ``operation`` (string) — ``create``, ``update``, or ``delete`` (default ``update``).
- ``note`` (string, optional) — short rationale shown beside the diff.

**Returns** a confirmation that the suggestion was queued. Nothing is written or
indexed until the operator approves it (ADR-0033).

## Projects (knowledge bases)

The knowledge base is split into **projects** — top-level folders, each an independent
knowledge base. Documents are addressed ``<project>/<folder>/<doc>.md``. Navigate with the
read-only tools below; restructure with the propose tools (every change is reviewed).

## knowledge_list_projects

List the knowledge bases (projects). No parameters. Returns their names.

## knowledge_tree

Show the folder/document structure ("schema"). Optional ``project`` to scope to one;
omitted shows all. Returns an indented tree.

## knowledge_read_document

Read one document's full content. ``path`` is ``<project>/<folder>/<doc>.md``.

## knowledge_propose_move

Propose moving/renaming a document or folder (staged for review). Parameters: ``from_path``,
``to_path``, optional ``note``.

## knowledge_propose_rename

Propose renaming a document or folder in place (staged for review). Parameters: ``path``,
``new_name`` (a bare name, no ``/``; the ``.md`` suffix is kept for documents), optional
``note``.

## knowledge_propose_folder

Propose creating a folder (staged for review). Parameters: ``path`` (``<project>/<folder>``),
optional ``note``.

## knowledge_propose_project

Propose creating a new knowledge base (staged for review). Parameters: ``name`` (a single
folder name), optional ``note``.
""",
    },
]


def build_module(
    vault_indexer: KnowledgeIndexer,
    docs_indexer: KnowledgeIndexer,
    module_docs_indexer: ModuleDocsIndexer,
    suggestions: SuggestionStore,
    review: SuggestionReview,
    platform: PlatformClient,
    *,
    tenant: str,
    vault_path: Path,
    reader: VaultReader,
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
        review: Applies a staged change when the operator has review turned off.
        platform: Reads the suggestions-review on/off setting (#KB-refactor).
        tenant: The tenant whose suggestion queue the propose tool writes to.
        vault_path: Vault root — the (filesystem-independent) boundary the propose tools
            path-confine an edit target against (``safe_*``; no mount needed to validate).
        reader: The vault read backend (#346, ADR-0070) the agent's read tools —
            ``knowledge_list_projects`` / ``knowledge_tree`` / ``knowledge_read_document`` —
            and the create-tool existence guard use, so they read through the core file API.
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.21.0",
        description=(
            "Obsidian vault RAG + platform self-documentation: semantic search,"
            " incremental indexing, and multi-project knowledge bases."
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
        # Holds embeddings (vault + platform/module docs): re-embed on demand when the
        # embedding model changes, via POST /reindex (the core's re-embed fan-out, #332).
        reindexable=True,
    )

    module.emits(INDEX_COMPLETE_SUBJECT, "published after each incremental index run")

    async def _finalize(sid: str, applied_msg: str, pending_msg: str) -> str:
        """Leave a staged change pending under review, or auto-apply it when review is off.

        The operator can turn review off per module (#KB-refactor) — then the agent's change
        is applied immediately (the suggestion is approved right after it is staged), reusing
        the same apply path the operator would. A failed auto-apply (e.g. a read-only watched
        vault) leaves the change staged rather than losing it.
        """
        try:
            review_on = await platform.get_suggestions_enabled()
        except Exception:
            review_on = True  # if the setting can't be read, default to the safe (review) path
        if review_on:
            return tool_envelope(pending_msg, [])
        try:
            await review.approve(sid)
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"{pending_msg} (review is off but applying failed: {detail})", [])
        return tool_envelope(applied_msg, [])

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

    async def _stage_doc_write(
        op: str, path: str, content: str, note: str, *, reject_existing: bool = False
    ) -> str:
        """Path-confine, stage a create/update/delete suggestion, and finalize it.

        Shared by ``knowledge_create_document`` and ``knowledge_propose_edit`` so both take the
        exact same review path — staged for the operator's approval, or applied directly when
        review is off. ``reject_existing`` guards the create tool against clobbering a note that
        already exists.
        """
        try:
            # Path-confine to the vault (``.md`` only, no traversal) before staging it.
            rel = safe_vault_rel(path)
        except Exception as exc:  # HTTPException(detail=...) from safe_vault_rel
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"Cannot propose change to {path!r}: {detail}", [])
        if reject_existing and await reader.exists(rel):
            return tool_envelope(
                f"{path!r} already exists — use knowledge_propose_edit to update it instead.",
                [],
            )
        proposed = "" if op == "delete" else content
        suggestion = await suggestions.add(
            tenant=tenant,
            path=path,
            operation=op,
            proposed_content=proposed,
            origin="agent",
            note=note,
        )
        return await _finalize(
            suggestion.sid,
            f"{op.capitalize()} of '{path}' applied directly — review is off.",
            f"Proposed {op} of '{path}' (suggestion {suggestion.sid[:8]}). It is pending"
            " your review in Knowledge → Suggestions; nothing changes until you approve it.",
        )

    @module.tool()
    async def knowledge_create_document(path: str, content: str, note: str = "") -> str:
        """Create a NEW document in the knowledge base.

        This is the single-purpose tool to **add** a note/document — reach for it whenever you
        want to write a new note. The document is staged for the operator to review
        (Knowledge → Suggestions) and is written + indexed on approval; when review is off it is
        created immediately. To change an existing note use ``knowledge_propose_edit``; for
        folders or knowledge bases use ``knowledge_propose_folder`` / ``knowledge_propose_project``.

        Args:
            path: Vault-relative path of the new note, e.g. ``projects/goals.md``. Must end in
                ``.md``, stay inside the vault (no ``..`` traversal), and not already exist.
            content: The full markdown content of the new document.
            note: An optional short rationale shown to the operator alongside the diff.

        Returns confirmation that the document was created (or queued for review), or an error
        describing why the path was rejected (e.g. it already exists).
        """
        return await _stage_doc_write("create", path, content, note, reject_existing=True)

    @module.tool()
    async def knowledge_propose_edit(
        path: str,
        content: str = "",
        operation: str = "update",
        note: str = "",
    ) -> str:
        """Update or delete an existing vault note, staged for the operator to review (ADR-0033).

        This does **not** modify the vault directly. The change is staged as a suggestion the
        operator approves or rejects in the **Knowledge → Suggestions** page; only on approval
        is it written and indexed (when review is off it is applied immediately).

        To **create** a new note, prefer ``knowledge_create_document`` — the single-purpose
        create tool. This tool still accepts ``operation="create"`` for completeness.

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
        if op not in ("create", "update", "delete"):
            return tool_envelope(
                "knowledge_propose_edit handles create/update/delete only; use"
                " knowledge_propose_move, knowledge_propose_folder, or"
                " knowledge_propose_project for structural changes.",
                [],
            )
        return await _stage_doc_write(op, path, content, note)

    # ── Navigation (read-only): how the agent learns where things live ───────────

    @module.tool()
    async def knowledge_list_projects() -> str:
        """List the knowledge bases (projects) — the top-level collections of the KB.

        Each knowledge base is an independent set of notes/folders. Use this to discover
        what exists before reading, organising, or proposing changes. A document inside a
        knowledge base is addressed as ``<project>/<path>.md``.
        """
        projects = await reader.projects()
        if not projects:
            return "No knowledge bases yet. Propose one with knowledge_propose_project(name)."
        return "Knowledge bases:\n" + "\n".join(f"- {p}" for p in projects)

    @module.tool()
    async def knowledge_tree(project: str = "") -> str:
        """Show the folder/document structure of the knowledge base — its schema.

        Pass a *project* (knowledge base) name for just that one; omit it to see every
        knowledge base. Use this to learn where notes live so you can read them, decide
        where new notes belong, or plan a move. Paths are ``<project>/<folder>/<doc>.md``.
        """
        projects = [project.strip()] if project.strip() else await reader.projects()
        if not projects:
            return "No knowledge bases yet."
        lines: list[str] = []
        for proj in projects:
            lines.append(f"{proj}/")
            nodes = await reader.tree(subdir=proj)
            if not nodes:
                lines.append("  (empty)")
            for node in nodes:
                depth = node["path"].count("/") + 1
                name = node["path"].split("/")[-1]
                suffix = "/" if node["type"] == "dir" else ""
                lines.append(f"{'  ' * depth}{name}{suffix}")
        return "\n".join(lines)

    @module.tool()
    async def knowledge_read_document(path: str) -> str:
        """Read a knowledge-base document's full content by its path.

        *path* is ``<project>/<folder>/<doc>.md`` (discover it via knowledge_tree /
        knowledge_list_projects). Use this to read a note whose location you already know;
        use knowledge_search for semantic lookup. Returns an error if the path is invalid
        or the document does not exist.
        """
        try:
            rel = safe_vault_rel(path)
        except Exception as exc:  # HTTPException(detail=...) from safe_vault_rel
            detail = getattr(exc, "detail", str(exc))
            return f"Cannot read {path!r}: {detail}"
        content = await reader.read_text(rel)
        if content is None:
            return f"No such document: {path}"
        return content

    # ── Structural changes (staged for review, like every agent write) ───────────

    @module.tool()
    async def knowledge_propose_move(from_path: str, to_path: str, note: str = "") -> str:
        """Propose moving or renaming a document or folder, for operator review (ADR-0033).

        Staged as a suggestion — nothing moves until the operator approves it. Use this to
        reorganise the knowledge base: move a note into a folder, rename it, or move a whole
        folder. Paths are knowledge-base-relative (``<project>/<path>``).

        Args:
            from_path: The current path of the document or folder.
            to_path: The destination path.
            note: Optional short rationale shown to the operator.
        """
        src, dst = from_path.strip(), to_path.strip()
        if not src or not dst:
            return tool_envelope("Both from_path and to_path are required.", [])
        try:
            safe_dir_relative(vault_path, src)
            safe_dir_relative(vault_path, dst)
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"Cannot propose move: {detail}", [])
        suggestion = await suggestions.add(
            tenant=tenant,
            path=src,
            operation="move",
            proposed_content="",
            origin="agent",
            note=note,
            to_path=dst,
        )
        return await _finalize(
            suggestion.sid,
            f"Moved '{src}' to '{dst}' directly — review is off.",
            f"Proposed move of '{src}' to '{dst}' (suggestion {suggestion.sid[:8]}). Pending"
            " your review in Knowledge → Suggestions; nothing moves until you approve it.",
        )

    @module.tool()
    async def knowledge_propose_rename(path: str, new_name: str, note: str = "") -> str:
        """Propose renaming a document or folder (keeps it where it is), for review (ADR-0033).

        A convenience over knowledge_propose_move: supply the item's *path* and just the new
        leaf *new_name* (no slashes) — the same folder is kept. For a ``.md`` document the
        ``.md`` suffix is preserved. Staged as a move suggestion; applied only on approval.

        Args:
            path: The current path, ``<project>/<…>/<name>``.
            new_name: The new leaf name (no ``/``).
            note: Optional short rationale shown to the operator.
        """
        src = path.strip()
        leaf = new_name.strip()
        if not src or not leaf:
            return tool_envelope("Both path and new_name are required.", [])
        if "/" in leaf or "\\" in leaf:
            return tool_envelope(
                "new_name must be a bare name (no '/'); use knowledge_propose_move to relocate.",
                [],
            )
        if src.endswith(".md") and not leaf.endswith(".md"):
            leaf = f"{leaf}.md"
        parent = src.rsplit("/", 1)[0] if "/" in src else ""
        dst = f"{parent}/{leaf}" if parent else leaf
        try:
            safe_dir_relative(vault_path, src)
            safe_dir_relative(vault_path, dst)
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"Cannot propose rename: {detail}", [])
        suggestion = await suggestions.add(
            tenant=tenant,
            path=src,
            operation="move",
            proposed_content="",
            origin="agent",
            note=note,
            to_path=dst,
        )
        return await _finalize(
            suggestion.sid,
            f"Renamed '{src}' to '{dst}' directly — review is off.",
            f"Proposed rename of '{src}' to '{dst}' (suggestion {suggestion.sid[:8]}). Pending"
            " your review in Knowledge → Suggestions.",
        )

    @module.tool()
    async def knowledge_propose_folder(path: str, note: str = "") -> str:
        """Propose creating a folder in the knowledge base, for operator review (ADR-0033).

        Staged as a suggestion. *path* is ``<project>/<folder>``. (You can also just propose
        a document at a new path — its parent folders are created with it on approval.)
        """
        rel = path.strip()
        try:
            safe_dir_relative(vault_path, rel)
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"Cannot propose folder {path!r}: {detail}", [])
        suggestion = await suggestions.add(
            tenant=tenant,
            path=rel,
            operation="mkdir",
            proposed_content="",
            origin="agent",
            note=note,
        )
        return await _finalize(
            suggestion.sid,
            f"Created folder '{rel}' directly — review is off.",
            f"Proposed new folder '{rel}' (suggestion {suggestion.sid[:8]}). Pending your"
            " review in Knowledge → Suggestions.",
        )

    @module.tool()
    async def knowledge_propose_project(name: str, note: str = "") -> str:
        """Propose creating a new knowledge base (project), for operator review (ADR-0033).

        A knowledge base is a top-level collection of notes. Staged as a suggestion; it is
        created only on approval. *name* is a single folder name (no slashes).
        """
        try:
            safe_project(vault_path, name)
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return tool_envelope(f"Cannot propose knowledge base {name!r}: {detail}", [])
        suggestion = await suggestions.add(
            tenant=tenant,
            path=name.strip(),
            operation="mkproject",
            proposed_content="",
            origin="agent",
            note=note,
        )
        return await _finalize(
            suggestion.sid,
            f"Created knowledge base '{name.strip()}' directly — review is off.",
            f"Proposed new knowledge base '{name.strip()}' (suggestion {suggestion.sid[:8]})."
            " Pending your review in Knowledge → Suggestions.",
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
