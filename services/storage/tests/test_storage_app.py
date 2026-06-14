"""Smoke tests — the ASGI app builds and exposes the expected routes."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("STORAGE_ROOT", "/tmp")


def test_app_exposes_ops_mcp_and_download_routes() -> None:
    from epicurus_storage.app import create_app

    app = create_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/manifest" in paths
    assert "/ingest" in paths
    assert "/download" in paths
    assert "/pages/{page_id}" in paths
    assert any(p.startswith("/mcp") for p in paths)


def test_settings_storage_root_from_env() -> None:
    import importlib

    import epicurus_storage.settings as mod

    importlib.reload(mod)
    from epicurus_storage.settings import StorageSettings

    s = StorageSettings(service_name="storage")
    # The env var was set to /tmp at module scope; verify it is picked up.
    assert s.storage_root.name == "tmp"
