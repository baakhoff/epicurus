"""Notification-center routes (#671): list/filter, unread count, mark read.

Core page (ADR-0018/0019: the shell renders, this supplies data) — no module UI, the same
"Settings-surface, not a module page" shape as scheduled turns / push. The durable record
itself is written by ``PushService.notify`` (``push/service.py``), never by these routes;
this router is read + read-state-mutation only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from epicurus_core_app.notifications import Notification, NotificationStore


class NotificationView(BaseModel):
    id: str
    category: str
    title: str
    body: str
    deep_link: str | None = None
    entity_ref: dict[str, Any] | None = None
    automation_id: str | None = None
    created_at: str
    read_at: str | None = None


class UnreadCountView(BaseModel):
    count: int


class MarkAllReadView(BaseModel):
    marked: int


def _view(n: Notification) -> NotificationView:
    return NotificationView(
        id=n.id,
        category=n.category,
        title=n.title,
        body=n.body,
        deep_link=n.deep_link,
        entity_ref=n.entity_ref,
        automation_id=n.automation_id,
        created_at=n.created_at.isoformat(),
        read_at=n.read_at.isoformat() if n.read_at else None,
    )


def create_notifications_router(
    store: NotificationStore, *, default_tenant: str = "local"
) -> APIRouter:
    """List/filter, unread-count, and mark-read for the notification center."""
    router = APIRouter(prefix="/platform/v1/notifications", tags=["notifications"])

    @router.get("", response_model=list[NotificationView])
    async def list_notifications(
        tenant_id: str | None = Query(None),
        category: str | None = Query(None),
        unread_only: bool = Query(False),
    ) -> list[NotificationView]:
        tenant = tenant_id or default_tenant
        rows = await store.list(tenant, category=category, unread_only=unread_only)
        return [_view(n) for n in rows]

    @router.get("/unread-count", response_model=UnreadCountView)
    async def unread_count(tenant_id: str | None = Query(None)) -> UnreadCountView:
        tenant = tenant_id or default_tenant
        return UnreadCountView(count=await store.unread_count(tenant))

    @router.post("/{notification_id}/read")
    async def mark_read(notification_id: str, tenant_id: str | None = Query(None)) -> Response:
        tenant = tenant_id or default_tenant
        ok = await store.mark_read(tenant=tenant, notification_id=notification_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such notification: {notification_id}")
        return Response(status_code=204)

    @router.post("/read-all", response_model=MarkAllReadView)
    async def mark_all_read(tenant_id: str | None = Query(None)) -> MarkAllReadView:
        tenant = tenant_id or default_tenant
        marked = await store.mark_all_read(tenant)
        return MarkAllReadView(marked=marked)

    return router


__all__ = [
    "MarkAllReadView",
    "NotificationView",
    "UnreadCountView",
    "create_notifications_router",
]
