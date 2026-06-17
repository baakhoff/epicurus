"""The echo ASGI app builds with its ops + MCP routes (no startup required)."""

from __future__ import annotations

from epicurus_core import route_paths
from epicurus_echo.app import create_app


def test_app_exposes_ops_and_mcp_routes() -> None:
    app = create_app()
    paths = route_paths(app)
    assert "/health" in paths
    assert "/metrics" in paths
    assert any(p.startswith("/mcp") for p in paths)


def test_app_serves_the_declared_page_route() -> None:
    app = create_app()
    paths = route_paths(app)
    assert "/pages/{page_id}" in paths


def test_app_serves_the_resolver_route() -> None:
    app = create_app()
    paths = route_paths(app)
    assert "/resolve/{kind}/{ref_id}" in paths
