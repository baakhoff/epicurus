"""Knowledge-service configuration — CoreSettings plus knowledge-specific fields."""

from __future__ import annotations

from pathlib import Path

from epicurus_core import CoreSettings


class KnowledgeSettings(CoreSettings):
    """Adds vault path, Qdrant, database, and platform-API URL to shared settings."""

    # Absolute path inside the container to the Obsidian vault.
    vault_path: Path = Path("/vault")
    # Absolute path inside the container to the bundled platform docs (self-documentation).
    # Defaults to /docs, which is populated by COPY docs/ /docs in the Dockerfile.
    # Override with DOCS_PATH to bind-mount a live docs tree (repo-based stacks).
    docs_path: Path = Path("/docs")
    # Async Postgres DSN for the note hash/mtime tracking index.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
    # Qdrant endpoint for the vector index.
    qdrant_url: str = "http://localhost:6333"
    # Core service base URL (platform API).  On the Docker network: http://core:8080.
    platform_url: str = "http://localhost:8080"
    # Maximum number of characters per chunk before hard-splitting at paragraph boundaries.
    chunk_max_chars: int = 2000
