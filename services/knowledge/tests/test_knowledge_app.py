"""Smoke tests — the ASGI app builds and exposes the expected routes."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("VAULT_PATH", "/tmp")
os.environ.setdefault("DOCS_PATH", "/tmp")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("PLATFORM_URL", "http://localhost:8080")


def test_app_exposes_ops_mcp_manifest_and_status_routes() -> None:
    from epicurus_knowledge.app import create_app

    app = create_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/manifest" in paths
    assert "/status" in paths
    assert "/pages/{page_id}" in paths  # editor page list (#130)
    assert "/pages/{page_id}/doc" in paths  # editor doc read/write (#130)
    assert any(p.startswith("/mcp") for p in paths)


async def test_manifest_declares_editor_page() -> None:
    from epicurus_knowledge.service import build_module

    module = build_module(_indexer_stub(), _indexer_stub())
    manifest = await module.manifest()
    assert [p.id for p in manifest.pages] == ["vault"]
    assert manifest.pages[0].archetype == "editor"
    assert manifest.pages[0].title == "Knowledge"
    assert manifest.version == "0.4.0"


def _indexer_stub() -> object:
    """A do-nothing stand-in for KnowledgeIndexer — build_module only stores it."""
    from unittest.mock import MagicMock

    return MagicMock()


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
