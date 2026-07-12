"""Scheduled-turns routes — Settings-surface CRUD (list/create/pause/delete), shell-rendered.

No module UI (ADR-0018): this is core-owned Settings territory, the same shape as the
timezone and instructions routes, not a `pages` archetype any module declares.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from epicurus_core_app.scheduled_turns import ScheduledTurn, ScheduledTurnStore, validate_cadence


class ScheduledTurnView(BaseModel):
    """The API-facing shape of a scheduled turn."""

    id: str
    prompt: str
    cadence: str
    hour: int
    weekday: int | None = None
    delivery_target: str
    enabled: bool
    created_at: str
    last_run_at: str | None = None
    last_status: str | None = None


class CreateScheduledTurnRequest(BaseModel):
    """Body for ``POST /platform/v1/scheduled-turns``."""

    prompt: str
    cadence: str
    hour: int
    weekday: int | None = None  # 0=Monday..6=Sunday; required when cadence == "weekly"


class SetEnabledBody(BaseModel):
    """Body for ``POST /platform/v1/scheduled-turns/{id}/enabled``."""

    enabled: bool


def _view(turn: ScheduledTurn) -> ScheduledTurnView:
    return ScheduledTurnView(
        id=turn.id,
        prompt=turn.prompt,
        cadence=turn.cadence,
        hour=turn.hour,
        weekday=turn.weekday,
        delivery_target=turn.delivery_target,
        enabled=turn.enabled,
        created_at=turn.created_at.isoformat(),
        last_run_at=turn.last_run_at.isoformat() if turn.last_run_at else None,
        last_status=turn.last_status,
    )


def create_scheduled_turns_router(
    store: ScheduledTurnStore, *, default_tenant: str = "local"
) -> APIRouter:
    """List/create/pause/delete scheduled turns (Settings surface, no module page)."""
    router = APIRouter(prefix="/platform/v1/scheduled-turns", tags=["scheduled-turns"])

    @router.get("", response_model=list[ScheduledTurnView])
    async def list_turns(tenant_id: str | None = Query(None)) -> list[ScheduledTurnView]:
        tenant = tenant_id or default_tenant
        return [_view(t) for t in await store.list(tenant=tenant)]

    @router.post("", response_model=ScheduledTurnView)
    async def create_turn(
        body: CreateScheduledTurnRequest, tenant_id: str | None = Query(None)
    ) -> ScheduledTurnView:
        prompt = body.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt must not be blank")
        if not (0 <= body.hour <= 23):
            raise HTTPException(status_code=400, detail="hour must be 0-23")
        try:
            validate_cadence(body.cadence, body.weekday)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tenant = tenant_id or default_tenant
        # A fresh, dedicated session per scheduled turn: it comes into being — and gets a
        # sensible title, derived from its first message (the prompt itself) — the moment the
        # turn first runs, exactly like any other session (no separate "create session" step).
        delivery_target = f"scheduled-{uuid.uuid4().hex}"
        turn = await store.create(
            tenant=tenant,
            prompt=prompt,
            cadence=body.cadence,
            hour=body.hour,
            weekday=body.weekday,
            delivery_target=delivery_target,
        )
        return _view(turn)

    @router.post("/{turn_id}/enabled", response_model=dict[str, object])
    async def set_enabled(
        turn_id: str, body: SetEnabledBody, tenant_id: str | None = Query(None)
    ) -> dict[str, object]:
        tenant = tenant_id or default_tenant
        ok = await store.set_enabled(tenant=tenant, turn_id=turn_id, enabled=body.enabled)
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such scheduled turn: {turn_id}")
        return {"status": "ok", "enabled": body.enabled}

    @router.delete("/{turn_id}")
    async def delete_turn(turn_id: str, tenant_id: str | None = Query(None)) -> Response:
        tenant = tenant_id or default_tenant
        ok = await store.delete(tenant=tenant, turn_id=turn_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such scheduled turn: {turn_id}")
        return Response(status_code=204)

    return router


__all__ = [
    "CreateScheduledTurnRequest",
    "ScheduledTurnView",
    "SetEnabledBody",
    "create_scheduled_turns_router",
]
