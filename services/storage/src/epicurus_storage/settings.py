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
    # Async Postgres DSN used for the file index.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
    # MinIO (S3-compatible) endpoint and credentials for app-managed objects.
    minio_url: str = "http://minio:9000"
    minio_access_key: str = "epicurus"
    minio_secret_key: str = "epicurus-dev"
