"""Notes-service configuration — CoreSettings plus notes-specific fields."""

from __future__ import annotations

from pathlib import Path

from epicurus_core import CoreSettings


class NotesSettings(CoreSettings):
    """Adds Postgres, Qdrant, and platform-API URLs to the shared settings."""

    # Async Postgres DSN — the source of truth for note bodies.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
    # Notes' folder in the shared file space (#KB-refactor, req 7): each saved note is
    # mirrored here as ``<slug>.md`` so the storage module shows notes in the Files view.
    # Postgres stays the source of truth; the mirror is read-only output.
    notes_root: Path = Path("/data/notes")
    # Qdrant endpoint for the per-tenant ``<tenant>__notes`` vector collection.
    qdrant_url: str = "http://localhost:6333"
    # Core service base URL (platform API) — embeddings come from the core's gateway
    # so the module never holds a model key (ADR-0010). On the Docker network this is
    # http://core-app:8080.
    platform_url: str = "http://localhost:8080"
    # Upper bound on characters per chunk before hard-splitting at paragraph boundaries.
    chunk_max_chars: int = 2000
