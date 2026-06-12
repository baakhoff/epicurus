"""Knowledge module — MCP tool surface.

Registers the ``knowledge_reindex`` tool that the agent can call to trigger an
incremental re-scan of the Obsidian vault.  The semantic-search tool (issue #69)
will be added in the next issue once the index is proven.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, UiAction, UiSection
from epicurus_knowledge.indexer import KnowledgeIndexer

MODULE_NAME = "knowledge"

INDEX_COMPLETE_SUBJECT = "knowledge.index.completed"


def build_module(indexer: KnowledgeIndexer) -> EpicurusModule:
    """Build the knowledge module and register its tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
        description="Obsidian vault RAG: incremental index of notes into Qdrant.",
        ui=UiSection(
            summary="Indexes your Obsidian vault for semantic search by the agent.",
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
    async def knowledge_reindex() -> dict[str, int]:
        """Incrementally re-index the Obsidian vault.

        Walks the vault, embeds new or changed notes via the core's LLM gateway,
        and removes vectors for deleted notes.  Unchanged notes are skipped.

        Returns ``{"indexed": N, "deleted": M, "unchanged": K}`` where *N* notes
        were re-indexed, *M* were removed, and *K* were skipped as unchanged.
        """
        return await indexer.run()

    return module
