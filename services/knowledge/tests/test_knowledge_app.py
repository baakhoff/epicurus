"""Smoke tests — the ASGI app builds and exposes the expected routes."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("VAULT_PATH", "/tmp")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("PLATFORM_URL", "http://localhost:8080")


def test_app_exposes_ops_mcp_and_manifest_routes() -> None:
    from epicurus_knowledge.app import create_app

    app = create_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/manifest" in paths
    assert any(p.startswith("/mcp") for p in paths)


def test_settings_vault_path_from_env() -> None:
    import importlib

    import epicurus_knowledge.settings as mod

    importlib.reload(mod)
    from epicurus_knowledge.settings import KnowledgeSettings

    s = KnowledgeSettings(service_name="knowledge")
    assert s.vault_path.name == "tmp"
