"""Knowledge module — MCP tool surface.

Registers two tools the agent can call:

* ``knowledge_search`` — embed a query and return the top-k matching chunks
  from the indexed vault **and** the bundled platform docs, merged by score.
* ``knowledge_reindex`` — trigger an incremental re-scan of both sources.
"""

from __future__ import annotations

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
from epicurus_knowledge.pages import VAULT_PAGE_ID
from epicurus_knowledge.refs import (
    KNOWLEDGE_KIND,
    SOURCE_DOC,
    SOURCE_NOTE,
    doc_title,
    encode_ref,
)

MODULE_NAME = "knowledge"

INDEX_COMPLETE_SUBJECT = "knowledge.index.completed"


def build_module(
    vault_indexer: KnowledgeIndexer,
    docs_indexer: KnowledgeIndexer,
) -> EpicurusModule:
    """Build the knowledge module and register its tools.

    Args:
        vault_indexer: Indexer for the operator's Obsidian vault
            (``<tenant>__knowledge`` collection).
        docs_indexer: Indexer for the bundled platform docs
            (``<tenant>__docs`` collection).
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.7.0",
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
            )
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
                    description="Incrementally re-index the vault and platform docs.",
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
        """Incrementally re-index the Obsidian vault and the bundled platform docs.

        Walks each source directory, embeds new or changed files via the core's
        LLM gateway, and removes vectors for deleted files.  Unchanged files are
        skipped.

        Returns ``{"indexed": N, "deleted": M, "unchanged": K}`` summed across
        both sources.
        """
        vault_result = await vault_indexer.run()
        docs_result = await docs_indexer.run()
        return {
            "indexed": vault_result["indexed"] + docs_result["indexed"],
            "deleted": vault_result["deleted"] + docs_result["deleted"],
            "unchanged": vault_result["unchanged"] + docs_result["unchanged"],
        }

    return module


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
