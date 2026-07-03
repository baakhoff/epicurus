"""Timezone preference routes (ADR-0039).

A single editable setting — the operator's IANA timezone — backing the Settings screen
and the agent's ``now`` tool. Mirrors the llm-prefs routes.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from epicurus_core_app.timezone_prefs import TimezonePrefsStore


class TimezoneResponse(BaseModel):
    """The tenant's effective timezone (stored value, else the configured default)."""

    timezone: str


class SetTimezoneRequest(BaseModel):
    """Body for ``PUT /platform/v1/timezone``."""

    timezone: str


def create_timezone_router(
    timezone_prefs: TimezonePrefsStore | None = None,
    default_tenant: str = "local",
) -> APIRouter:
    """Read/set the operator's IANA timezone (ADR-0039)."""
    router = APIRouter(prefix="/platform/v1/timezone", tags=["timezone"])

    @router.get("", response_model=TimezoneResponse)
    async def get_timezone(tenant_id: str | None = Query(None)) -> TimezoneResponse:
        """Return the caller's effective timezone (falls back to the default tenant)."""
        if timezone_prefs is None:
            return TimezoneResponse(timezone="UTC")
        tenant = tenant_id or default_tenant
        return TimezoneResponse(timezone=await timezone_prefs.get_timezone(tenant))

    @router.put("")
    async def set_timezone(
        request: SetTimezoneRequest, tenant_id: str | None = Query(None)
    ) -> dict[str, str]:
        """Set the caller's timezone (validated as a real IANA zone; 400 otherwise)."""
        if timezone_prefs is None:
            raise HTTPException(status_code=503, detail="timezone store not available")
        try:
            ZoneInfo(request.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"unknown timezone {request.timezone!r}"
            ) from exc
        tenant = tenant_id or default_tenant
        await timezone_prefs.set_timezone(tenant, request.timezone)
        return {"status": "ok", "timezone": request.timezone}

    return router
