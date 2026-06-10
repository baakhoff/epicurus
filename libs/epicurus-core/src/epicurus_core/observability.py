"""The operational HTTP surface every service exposes: ``/health`` + ``/metrics``.

Kept tiny and dependency-light so every module mounts the same liveness and
Prometheus endpoints the gateway and observability stack expect.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client import REGISTRY as DEFAULT_REGISTRY
from pydantic import BaseModel

from epicurus_core._version import __version__

__all__ = ["HealthResponse", "add_ops_routes", "create_ops_router"]


class HealthResponse(BaseModel):
    """Liveness payload returned by ``GET /health``."""

    status: str
    service: str
    version: str


def create_ops_router(
    service_name: str,
    *,
    version: str = __version__,
    registry: CollectorRegistry = DEFAULT_REGISTRY,
) -> APIRouter:
    """Build a router exposing ``GET /health`` and ``GET /metrics``.

    ``version`` defaults to the ``epicurus-core`` version; pass the service's own
    version (e.g. via :func:`importlib.metadata.version`) so ``/health`` reports it.
    """
    router = APIRouter(tags=["ops"])

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service=service_name, version=version)

    @router.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return router


def add_ops_routes(
    app: FastAPI,
    service_name: str,
    *,
    version: str = __version__,
    registry: CollectorRegistry = DEFAULT_REGISTRY,
) -> None:
    """Mount the ops router onto an existing FastAPI app."""
    app.include_router(create_ops_router(service_name, version=version, registry=registry))
