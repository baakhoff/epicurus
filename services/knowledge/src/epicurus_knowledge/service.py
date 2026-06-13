"""Knowledge module — MCP tool surface.

Registers two tools the agent can call:

* ``knowledge_search`` — embed a query and return the top-k matching chunks
  from the indexed vault **and** the bundled platform docs, merged by score.
* ``knowledge_reindex`` — trigger an incremental re-scan of both sources.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, UiAction, UiSection
from epicurus_knowledge.indexer import KnowledgeIndexer, SearchHit

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
        version="0.3.0",
        description=(
            "Obsidian vault RAG + platform self-documentation: semantic search"
            " and incremental indexing."
        ),
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
    )

    module.emits(INDEX_COMPLETE_SUBJECT, "published after each incremental index run")

    @module.tool()
    async def knowledge_search(query: str, k: int = 5) -> list[SearchHit]:
        """Search the knowledge base for content relevant to *query*.

        Searches both the operator's Obsidian vault (``<tenant>__knowledge``)
        and the bundled platform docs (``<tenant>__docs``).  Results from both
        sources are merged and re-ranked by cosine similarity score, returning
        the top *k* across both collections.

        The ``note_path`` field in results from the platform docs is prefixed
        with ``docs/`` (e.g. ``docs/services/knowledge.md``) so the agent can
        distinguish them from vault notes.

        Args:
            query: Natural-language question or search phrase.
            k: Maximum number of chunks to return (default 5).

        Returns a list of ``{note_path, heading, text, score}`` dicts ordered by
        descending relevance.  Returns an empty list when neither source has been
        indexed yet.
        """
        vault_hits, docs_hits = await _search_both(vault_indexer, docs_indexer, query, k)
        merged = sorted(vault_hits + docs_hits, key=lambda h: h["score"], reverse=True)
        return merged[:k]

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
