"""``route_paths`` flattens FastAPI's route tree across the 0.137 lazy-include change."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from epicurus_core import route_paths


def test_includes_nested_router_routes() -> None:
    app = FastAPI()
    router = APIRouter()

    @router.get("/health")
    def _health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    assert "/health" in route_paths(app)


def test_applies_router_prefix() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/platform/v1")  # the prefix style used across the services

    @router.get("/info")
    def _info() -> dict[str, str]:
        return {}

    app.include_router(router)
    assert "/platform/v1/info" in route_paths(app)


def test_includes_mounts() -> None:
    app = FastAPI()
    app.mount("/mcp", FastAPI())
    assert any(p.startswith("/mcp") for p in route_paths(app))
