"""Storage-service configuration — CoreSettings plus storage-specific fields."""

from __future__ import annotations

from pathlib import Path

from epicurus_core import CoreSettings

# Maximum bytes the storage_read tool will return inline (256 KB).
READ_MAX_BYTES = 256 * 1024


class StorageSettings(CoreSettings):
    """Adds the root directory, database DSN, and MinIO config to shared settings."""

    # Absolute path to the read-only directory tree to index and serve.
    storage_root: Path = Path("/data")
    # Top-level subtrees hidden from the AGENT's file tools (comma-separated, #KB-refactor).
    # The notes module mirrors private notes under `notes/`; the agent must not read them via
    # storage_read/search/list, though the operator still browses them in the Files page.
    agent_hidden_prefixes: str = "notes"
    # Async Postgres DSN used for the file index.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
    # MinIO (S3-compatible) endpoint and credentials for app-managed objects.
    minio_url: str = "http://minio:9000"
    minio_access_key: str = "epicurus"
    minio_secret_key: str = "epicurus-dev"
    # Live files-tree sync (#390): watch the served tree and incrementally rescan on change
    # so files another module / sync / external write lands after startup show up in the
    # Files view and name search without a restart or a manual storage_rescan. On by default
    # — this fixes a real stale-index bug, so watching the shared file space is the intended
    # behaviour; operators can disable it (e.g. a huge tree where startup-only is enough) with
    # STORAGE_WATCH=false. The watcher only reads the read-only tree (ADR-0057).
    storage_watch: bool = True
    # Coalescing window (milliseconds) for a burst of file changes before a rescan fires. A
    # module dropping many files at once is grouped into one window, keeping a burst to a
    # single incremental pass. Passed to the watcher's debounce.
    storage_watch_debounce_ms: int = 1500
