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
    # How many chunk texts to embed per platform-API round-trip (#230). The indexer
    # accumulates chunks across files and flushes a batch once this many are pending,
    # cutting the bundled-docs index from one HTTP call per file to one per batch.
    embed_batch_size: int = 64
    # Initial index resilience (#230): the first index runs in the background with
    # retry/backoff so a cold `compose up` (deps not yet ready) still ends populated.
    index_retry_max_attempts: int = 30
    index_retry_base_delay_seconds: float = 1.0
    index_retry_max_delay_seconds: float = 30.0
