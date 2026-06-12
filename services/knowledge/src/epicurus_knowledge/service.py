"""Knowledge module — MCP tool surface.

Registers two tools the agent can call:

* ``knowledge_search`` — embed a query and return the top-k matching chunks
  from the indexed vault, with source note path and section heading.
* ``knowledge_reindex`` — trigger an incremental re-scan of the Obsidian vault.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, UiAction, UiSection
from epicurus_knowledge.indexer import KnowledgeIndexer, SearchHit

MODULE_NAME = "knowledge"

INDEX_COMPLETE_SUBJECT = "knowledge.index.completed"


def build_module(indexer: KnowledgeIndexer) -> EpicurusModule:
    """Build the knowledge module and register its tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.2.0",
        description="Obsidian vault RAG: semantic search and incremental indexing.",
        ui=UiSection(
            icon="book",
            summary=(
                "Indexes your Obsidian vault so the agent can answer questions"
                " grounded in your notes."
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
                    label="Re-index vault",
                    description="Incrementally re-index all notes, updating only what changed.",
                )
            ],
        ),
    )

    module.emits(INDEX_COMPLETE_SUBJECT, "published after each incremental index run")

    @module.tool()
    async def knowledge_search(query: str, k: int = 5) -> list[SearchHit]:
        """Search the indexed vault for notes relevant to *query*.

        Embeds *query* via the core's LLM gateway, queries the tenant's Qdrant
        collection for the closest chunks, and returns the top *k* results with
        their source note path and section heading so the agent can cite them.

        Args:
            query: Natural-language question or search phrase.
            k: Maximum number of chunks to return (default 5).

        Returns a list of ``{note_path, heading, text, score}`` dicts ordered by
        descending relevance.  Returns an empty list when the vault has not been
        indexed yet.
        """
        return await indexer.search(query, k)

    @module.tool()
    async def knowledge_reindex() -> dict[str, int]:
        """Incrementally re-index the Obsidian vault.

        Walks the vault, embeds new or changed notes via the core's LLM gateway,
        and removes vectors for deleted notes.  Unchanged notes are skipped.

        Returns ``{"indexed": N, "deleted": M, "unchanged": K}`` where *N* notes
        were re-indexed, *M* were removed, and *K* were skipped as unchanged.
        """
        return await indexer.run()

    return module
