"""The echo ASGI app builds with its ops + MCP routes (no startup required)."""

from __future__ import annotations

from epicurus_echo.app import create_app


def test_app_exposes_ops_and_mcp_routes() -> None:
    app = create_app()
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/health" in paths
    assert "/metrics" in paths
    assert any(p.startswith("/mcp") for p in paths)
