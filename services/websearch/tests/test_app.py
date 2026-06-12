"""Smoke tests — the ASGI app builds and exposes the expected routes."""

from __future__ import annotations

import os

os.environ.setdefault("SEARXNG_URL", "http://localhost:8080")
os.environ.setdefault("PLATFORM_URL", "http://localhost:8080")


def test_app_exposes_ops_mcp_manifest_and_status_routes() -> None:
    from epicurus_websearch.app import create_app

    app = create_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/manifest" in paths
    assert "/status" in paths
    assert any(p.startswith("/mcp") for p in paths)


def test_settings_searxng_url_from_env() -> None:
    import importlib

    import epicurus_websearch.settings as mod

    importlib.reload(mod)
    from epicurus_websearch.settings import WebSearchSettings

    s = WebSearchSettings(service_name="websearch")
    assert "localhost" in s.searxng_url


def test_settings_max_results_default() -> None:
    from epicurus_websearch.settings import WebSearchSettings

    s = WebSearchSettings(service_name="websearch")
    assert s.websearch_max_results == 5
