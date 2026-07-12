"""Left-nav page-order routes (#543).

A single tenant-scoped setting — the operator's drag-and-drop order for module-contributed
left-nav pages — backing the Modules screen's reorder UI and the shell's nav sort. Mirrors
the timezone/llm-prefs routes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from epicurus_core_app.page_order_prefs import PageOrderStore


class PageOrderResponse(BaseModel):
    """The tenant's stored page-id order (`[]` means no preference set yet)."""

    order: list[str]


class SetPageOrderRequest(BaseModel):
    """Body for ``PUT /platform/v1/page-order``."""

    order: list[str]


def create_page_order_router(
    page_order_prefs: PageOrderStore | None = None,
    default_tenant: str = "local",
) -> APIRouter:
    """Read/set the operator's left-nav page order (#543)."""
    router = APIRouter(prefix="/platform/v1/page-order", tags=["page-order"])

    @router.get("", response_model=PageOrderResponse)
    async def get_page_order(tenant_id: str | None = Query(None)) -> PageOrderResponse:
        """Return the caller's stored page-id order (`[]` if unset)."""
        if page_order_prefs is None:
            return PageOrderResponse(order=[])
        tenant = tenant_id or default_tenant
        return PageOrderResponse(order=await page_order_prefs.get_order(tenant))

    @router.put("")
    async def set_page_order(
        request: SetPageOrderRequest, tenant_id: str | None = Query(None)
    ) -> PageOrderResponse:
        """Persist the caller's page-id order, replacing any prior list."""
        if page_order_prefs is None:
            raise HTTPException(status_code=503, detail="page-order store not available")
        tenant = tenant_id or default_tenant
        await page_order_prefs.set_order(tenant, request.order)
        return PageOrderResponse(order=request.order)

    return router
