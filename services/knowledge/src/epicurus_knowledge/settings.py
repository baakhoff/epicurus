"""Knowledge-service configuration — CoreSettings plus knowledge-specific fields."""

from __future__ import annotations

from pathlib import Path

from epicurus_core import CoreSettings


class KnowledgeSettings(CoreSettings):
    """Adds vault path, Qdrant, database, and platform-API URL to shared settings."""

    # Knowledge's root inside the shared file space (#KB-refactor): each top-level folder
    # under it is a "project" (knowledge base). Lives under the same /data tree the storage
    # module indexes read-only, so knowledge documents show up in the Files view.
    vault_path: Path = Path("/data/knowledge")
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
    # Live vault sync (#232): when an externally-synced vault is bind-mounted (e.g. an
    # Obsidian Sync folder), watch it and incrementally re-index on change so edits
    # landed on disk show up in search without a manual re-index. Opt-in — off by default
    # so the common image-only / empty-volume deploy keeps no watcher. Enabling it also
    # marks the vault **externally owned**: the editor page goes read-only and agent
    # suggestions can't be applied, leaving Obsidian (or whatever syncs the folder) the
    # sole author (ADR-0035).
    vault_watch: bool = False
    # Coalescing window (milliseconds) for a burst of vault changes before a re-index is
    # triggered. Obsidian Sync writes many files at once; grouping them into one window
    # keeps a burst to a single incremental pass. Passed to the watcher's debounce.
    vault_watch_debounce_ms: int = 1500

    @property
    def vault_read_only(self) -> bool:
        """Whether epicurus treats the vault as externally owned and never writes it.

        Tied to watch mode (#232, ADR-0035): a watched external vault has a second author
        (Obsidian / the syncing process), so epicurus-side writes — the editor save, the
        file-tree CRUD, and applying an agent suggestion — are disabled to avoid two
        writers racing on the same files. Obsidian Sync resolves conflicts within its own
        ecosystem; epicurus stays a pure reader of the synced folder.
        """
        return self.vault_watch
