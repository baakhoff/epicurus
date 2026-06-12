"""Storage-service configuration — CoreSettings plus storage-specific fields."""

from __future__ import annotations

from pathlib import Path

from epicurus_core import CoreSettings


class StorageSettings(CoreSettings):
    """Adds the root directory and database DSN to the shared settings."""

    # Absolute path to the read-only directory tree to index and serve.
    storage_root: Path = Path("/data")
    # Async Postgres DSN used for the file index.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
