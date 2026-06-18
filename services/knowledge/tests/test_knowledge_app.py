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
    assert manifest.version == "0.12.0"


async def test_manifest_declares_attachable_and_resolver() -> None:
    manifest = await _module().manifest()
    assert manifest.attachable is True  # vault docs can be attached to a chat (#137)
    assert manifest.resolver is True  # cited docs resolve to a hover-card (#143)
    assert manifest.docs_url == "/module-docs"  # contributes its own usage docs (#215)


def _indexer_stub() -> object:
    """A do-nothing stand-in for KnowledgeIndexer — build_module only stores it."""
    from unittest.mock import MagicMock

    return MagicMock()


def _module():  # type: ignore[no-untyped-def]
    """Build the module for manifest assertions (suggestion store never exercised here)."""
    from pathlib import Path

    from sqlalchemy.ext.asyncio import create_async_engine

    from epicurus_knowledge.service import build_module
    from epicurus_knowledge.suggestions import SuggestionStore

    store = SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    return build_module(
        _indexer_stub(),
        _indexer_stub(),
        _indexer_stub(),
        store,
        tenant="test",
        vault_path=Path("/vault"),
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
