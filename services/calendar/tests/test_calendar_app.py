"""The calendar ASGI app wires its ops, status, page, and MCP routes (no startup)."""

from __future__ import annotations

from epicurus_calendar.app import create_app


def test_app_exposes_ops_status_and_page_routes() -> None:
    app = create_app()
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/status" in paths
    assert "/pages/{page_id}" in paths
    assert any(p.startswith("/mcp") for p in paths)
