"""Agent system-prompt (instructions) routes (#497, ADR-0083).

A single editable setting — the operator's base system prompt — backing the Settings screen
and injected as the first system message of every agent turn. Mirrors the timezone routes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from epicurus_core_app.agent.instructions import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AgentInstructionsStore,
)


class InstructionsResponse(BaseModel):
    """The tenant's effective agent instructions plus whether it's the shipped default."""

    instructions: str
    # True when no custom prompt is stored (the effective value is the shipped default) — drives
    # the Settings editor's "Reset to default" affordance.
    is_default: bool


class SetInstructionsRequest(BaseModel):
    """Body for ``PUT /platform/v1/agent/instructions``. ``null``/blank resets to the default."""

    instructions: str | None


def create_instructions_router(
    instructions: AgentInstructionsStore | None = None,
    default_tenant: str = "local",
) -> APIRouter:
    """Read/set the operator's base system prompt (#497, ADR-0083)."""
    router = APIRouter(prefix="/platform/v1/agent/instructions", tags=["agent"])

    @router.get("", response_model=InstructionsResponse)
    async def get_instructions(tenant_id: str | None = Query(None)) -> InstructionsResponse:
        """Return the caller's effective prompt (stored value, else the shipped default)."""
        if instructions is None:
            return InstructionsResponse(instructions=DEFAULT_AGENT_INSTRUCTIONS, is_default=True)
        tenant = tenant_id or default_tenant
        raw = await instructions.get_raw(tenant)
        effective = raw if raw is not None else instructions.default
        return InstructionsResponse(instructions=effective, is_default=raw is None)

    @router.put("")
    async def set_instructions(
        request: SetInstructionsRequest, tenant_id: str | None = Query(None)
    ) -> dict[str, object]:
        """Set the caller's prompt, or reset it (a blank/``null`` body clears the override)."""
        if instructions is None:
            raise HTTPException(status_code=503, detail="instructions store not available")
        tenant = tenant_id or default_tenant
        await instructions.set_instructions(tenant, request.instructions)
        raw = await instructions.get_raw(tenant)
        return {"status": "ok", "is_default": raw is None}

    return router
