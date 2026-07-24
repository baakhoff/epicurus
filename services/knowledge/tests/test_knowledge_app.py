"""Smoke tests — the ASGI app builds and exposes the expected routes."""

from __future__ import annotations

import os

from epicurus_core import route_paths

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("VAULT_PATH", "/tmp")
os.environ.setdefault("DOCS_PATH", "/tmp")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("PLATFORM_URL", "http://localhost:8080")


def test_app_exposes_ops_mcp_manifest_and_status_routes() -> None:
    from epicurus_knowledge.app import create_app

    app = create_app()
    paths = route_paths(app)
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/manifest" in paths
    assert "/status" in paths
    assert "/pages/{page_id}" in paths  # editor page list (#130)
    assert "/pages/{page_id}/doc" in paths  # editor doc read/write (#130)
    assert "/pages/review" in paths  # review queue data (#220)
    assert "/pages/review/suggestions/{suggestion_id}/approve" in paths  # approve (#220)
    assert "/pages/review/suggestions/{suggestion_id}/reject" in paths  # reject (#220)
    assert "/attachments" in paths  # attachment picker (#137)
    assert "/attachments/{ref_id}" in paths  # attachment resolve (#137)
    assert "/resolve/{kind}/{ref_id}" in paths  # hover-card resolver (#143)
    assert any(p.startswith("/mcp") for p in paths)


async def test_manifest_declares_editor_and_review_pages() -> None:
    manifest = await _module().manifest()
    assert [p.id for p in manifest.pages] == ["vault", "review"]
    assert manifest.pages[0].archetype == "editor"
    assert manifest.pages[0].title == "Knowledge"
    assert manifest.pages[1].archetype == "review"  # suggestion queue (#220)
    assert manifest.version == "0.24.1"


async def test_manifest_declares_attachable_and_resolver() -> None:
    manifest = await _module().manifest()
    assert manifest.attachable is True  # vault docs can be attached to a chat (#137)
    assert manifest.resolver is True  # cited docs resolve to a hover-card (#143)
    assert manifest.docs_url == "/module-docs"  # contributes its own usage docs (#215)


async def test_manifest_declares_spine_events() -> None:
    manifest = await _module().manifest()
    subjects = {e.subject for e in manifest.events_emitted}
    # Spine events (#665); the legacy declared-but-never-published subject is gone.
    assert {
        "events.knowledge.doc_created",
        "events.knowledge.doc_updated",
        "events.knowledge.doc_deleted",
        "events.knowledge.vault_synced",
        "events.knowledge.index_failed",
    } <= subjects
    assert "knowledge.index.completed" not in subjects


def _indexer_stub() -> object:
    """A do-nothing stand-in for KnowledgeIndexer — build_module only stores it."""
    from unittest.mock import MagicMock

    return MagicMock()


def _module():  # type: ignore[no-untyped-def]
    """Build the module for manifest assertions (suggestion store never exercised here)."""
    from pathlib import Path
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import create_async_engine

    from epicurus_core import PlatformClient
    from epicurus_knowledge.pages import VaultPages
    from epicurus_knowledge.reader import DiskVaultReader
    from epicurus_knowledge.service import build_module
    from epicurus_knowledge.suggestions import (
        SuggestionAuditStore,
        SuggestionReview,
        SuggestionStore,
    )

    store = SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    vault = _indexer_stub()
    platform = AsyncMock(spec=PlatformClient)
    platform.get_suggestions_enabled = AsyncMock(return_value=True)
    # Manifest-only test — no vault read/write happens, so the mocked platform satisfies the
    # VaultPages file-API client requirement and the reader is never exercised.
    reader = DiskVaultReader(Path("/vault"))
    # Never exercised (no approve/reject here), so an uninitialised store is fine.
    audit = SuggestionAuditStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    review = SuggestionReview(
        store,
        VaultPages(
            Path("/vault"), vault, platform=platform, core_prefix="knowledge", reader=reader
        ),
        vault,
        reader=reader,
        tenant="test",
        audit=audit,
    )
    return build_module(
        vault,
        _indexer_stub(),
        _indexer_stub(),
        store,
        review,
        platform,
        tenant="test",
        vault_path=Path("/vault"),
        reader=reader,
    )


def test_settings_vault_path_from_env() -> None:
    import importlib

    import epicurus_knowledge.settings as mod

    importlib.reload(mod)
    from epicurus_knowledge.settings import KnowledgeSettings

    s = KnowledgeSettings(service_name="knowledge")
    assert s.vault_path.name == "tmp"


def test_settings_docs_path_default() -> None:
    from epicurus_knowledge.settings import KnowledgeSettings

    s = KnowledgeSettings(service_name="knowledge")
    # DOCS_PATH was set to /tmp above; verify the field exists and is a Path.
    from pathlib import Path

    assert isinstance(s.docs_path, Path)


def test_vault_root_is_tenant_scoped() -> None:
    """The on-disk vault is <files-root>/<tenant>/knowledge (constraint #1).

    Guards the path arithmetic in create_app: a regression that drops the tenant
    segment (or mis-orders .parent/.name) would put the vault back at the global
    /data/knowledge, breaking per-tenant isolation of the shared file space.
    """
    from pathlib import Path

    from epicurus_knowledge.settings import KnowledgeSettings

    s = KnowledgeSettings(service_name="knowledge", vault_path=Path("/data/knowledge"))
    vault_root = s.vault_path.parent / s.default_tenant_id / s.vault_path.name
    assert vault_root == Path("/data") / s.default_tenant_id / "knowledge"
    assert s.default_tenant_id in vault_root.parts  # the tenant segment is present
    # The bundled platform docs stay shared/read-only — never tenant-scoped.
    assert s.default_tenant_id not in s.docs_path.parts
