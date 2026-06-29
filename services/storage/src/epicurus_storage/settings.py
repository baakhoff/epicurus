"""Storage-service configuration — CoreSettings plus storage-specific fields."""

from __future__ import annotations

from epicurus_core import CoreSettings

# Maximum bytes the storage_read tool will return inline (256 KB).
READ_MAX_BYTES = 256 * 1024


class StorageSettings(CoreSettings):
    """Adds the object-index DSN, MinIO config, and the core platform URL to shared settings."""

    # Top-level subtrees hidden from the AGENT's file tools (comma-separated, #KB-refactor).
    # The notes module mirrors private notes under `notes/`; the agent must not read them via
    # storage_read/search/list, though the operator still browses them in the core Files page.
    agent_hidden_prefixes: str = "notes"
    # Async Postgres DSN used for the object index.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
    # Core base URL — the agent file tools read the core-owned file space through the platform
    # API (ADR-0063); storage no longer mounts /data. Compose sets http://core-app:8080.
    platform_url: str = "http://localhost:8080"
    # MinIO (S3-compatible) endpoint and credentials for app-managed objects.
    minio_url: str = "http://minio:9000"
    minio_access_key: str = "epicurus"
    minio_secret_key: str = "epicurus-dev"
