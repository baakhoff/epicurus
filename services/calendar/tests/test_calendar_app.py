"""The calendar ASGI app wires its ops, status, page, and MCP routes (no startup)."""

from __future__ import annotations

from epicurus_calendar.app import create_app
from epicurus_core import route_paths


def test_app_exposes_ops_status_and_page_routes() -> None:
    app = create_app()
    paths = route_paths(app)
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/status" in paths
    assert "/pages/{page_id}" in paths
    assert any(p.startswith("/mcp") for p in paths)


def test_app_exposes_resolver_and_attachment_routes() -> None:
    # The entity-ref resolver and chat-attachment source the core proxies (ADR-0019).
    app = create_app()
    paths = route_paths(app)
    assert "/resolve/{kind}/{ref_id}" in paths
    assert "/attachments" in paths
    assert "/attachments/{ref_id}" in paths
