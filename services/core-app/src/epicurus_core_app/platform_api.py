"""The module-facing **platform API** (module -> core), versioned under ``/platform/v1``.

Modules reach core capabilities — secrets, events, storage, the agent / LLM gateway,
the tool registry — through this local-only API (ADR-0004), rather than wiring to the
backends themselves. This skeleton exposes the discovery surface; the capability
endpoints arrive with their Phase-1 cards.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from epicurus_core import CONTRACT_VERSION, CoreSettings, __version__


class PlatformInfo(BaseModel):
    """What a module learns about the core it is talking to."""

    contract_version: str
    core_version: str
    tenant: str


def create_platform_router(settings: CoreSettings) -> APIRouter:
    """Build the ``/platform/v1`` router that modules call into."""
    router = APIRouter(prefix="/platform/v1", tags=["platform"])

    @router.get("/info", response_model=PlatformInfo)
    def info() -> PlatformInfo:
        return PlatformInfo(
            contract_version=CONTRACT_VERSION,
            core_version=__version__,
            tenant=settings.default_tenant_id,
        )

    return router
