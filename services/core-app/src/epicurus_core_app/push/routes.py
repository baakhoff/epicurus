"""Push routes: VAPID key, per-device subscribe/unsubscribe, shared push/center prefs.

Browser-facing Settings surface (ADR-0018: core-owned, no module page) — the PWA's
push-subscribe flow and the Settings -> Push card call these directly. The send path
itself (``PushService.notify``) has no HTTP route — it's a core-internal contract (see
``push/service.py``); ``/test`` below exists only so a human can trigger one real push to
manually verify the whole pipeline, per this PR's acceptance criteria.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from epicurus_core_app.push.prefs import KNOWN_CATEGORIES, ChannelPrefs, PushPrefs, PushPrefsStore
from epicurus_core_app.push.service import PushService
from epicurus_core_app.push.subscriptions import PushSubscription, PushSubscriptionStore


class VapidKeyView(BaseModel):
    public_key: str


class SubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    device_label: str = ""


class SubscriptionView(BaseModel):
    id: str
    device_label: str
    created_at: str
    last_seen_at: str | None = None


class ChannelPrefsView(BaseModel):
    push: bool
    center: bool


class PushPrefsView(BaseModel):
    categories: dict[str, ChannelPrefsView]
    known_categories: list[dict[str, str]]
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str


class SetPrefsRequest(BaseModel):
    """Body for ``PUT /platform/v1/push/prefs``. Every field optional — send only what changed."""

    categories: dict[str, ChannelPrefsView] | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class TestNotificationRequest(BaseModel):
    category: str = "system"


class TestNotificationView(BaseModel):
    outcome: str
    sent_count: int
    pruned_count: int


def _subscription_view(sub: PushSubscription) -> SubscriptionView:
    return SubscriptionView(
        id=sub.id,
        device_label=sub.device_label,
        created_at=sub.created_at.isoformat(),
        last_seen_at=sub.last_seen_at.isoformat() if sub.last_seen_at else None,
    )


def _prefs_view(prefs: PushPrefs) -> PushPrefsView:
    # One entry per *known* category, defaulted — so the UI never has to merge stored
    # sparse overrides with defaults itself; it just renders what it's given.
    categories = {
        cat["id"]: ChannelPrefsView(
            push=prefs.categories.get(cat["id"], ChannelPrefs()).push,
            center=prefs.categories.get(cat["id"], ChannelPrefs()).center,
        )
        for cat in KNOWN_CATEGORIES
    }
    return PushPrefsView(
        categories=categories,
        known_categories=[dict(c) for c in KNOWN_CATEGORIES],
        quiet_hours_enabled=prefs.quiet_hours_enabled,
        quiet_hours_start=prefs.quiet_hours_start,
        quiet_hours_end=prefs.quiet_hours_end,
    )


def create_push_router(
    service: PushService,
    *,
    subscriptions: PushSubscriptionStore,
    prefs: PushPrefsStore,
    default_tenant: str = "local",
) -> APIRouter:
    """Push subscribe/unsubscribe + shared push/center prefs (Settings surface, no module page)."""
    router = APIRouter(prefix="/platform/v1/push", tags=["push"])

    @router.get("/vapid-public-key", response_model=VapidKeyView)
    async def vapid_public_key(tenant_id: str | None = Query(None)) -> VapidKeyView:
        tenant = tenant_id or default_tenant
        return VapidKeyView(public_key=await service.get_vapid_public_key(tenant))

    @router.get("/subscriptions", response_model=list[SubscriptionView])
    async def list_subscriptions(tenant_id: str | None = Query(None)) -> list[SubscriptionView]:
        tenant = tenant_id or default_tenant
        return [_subscription_view(s) for s in await subscriptions.list(tenant)]

    @router.post("/subscriptions", response_model=SubscriptionView)
    async def create_subscription(
        body: SubscribeRequest, tenant_id: str | None = Query(None)
    ) -> SubscriptionView:
        if not body.endpoint.strip() or not body.p256dh.strip() or not body.auth.strip():
            raise HTTPException(
                status_code=400, detail="endpoint, p256dh, and auth are all required"
            )
        tenant = tenant_id or default_tenant
        sub = await subscriptions.create_or_update(
            tenant=tenant,
            endpoint=body.endpoint,
            p256dh=body.p256dh,
            auth=body.auth,
            device_label=body.device_label,
        )
        return _subscription_view(sub)

    @router.delete("/subscriptions/{sub_id}")
    async def delete_subscription(sub_id: str, tenant_id: str | None = Query(None)) -> Response:
        tenant = tenant_id or default_tenant
        ok = await subscriptions.delete(tenant=tenant, sub_id=sub_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"no such subscription: {sub_id}")
        return Response(status_code=204)

    @router.get("/prefs", response_model=PushPrefsView)
    async def get_prefs(tenant_id: str | None = Query(None)) -> PushPrefsView:
        tenant = tenant_id or default_tenant
        return _prefs_view(await prefs.get(tenant))

    @router.put("/prefs", response_model=PushPrefsView)
    async def set_prefs(
        body: SetPrefsRequest, tenant_id: str | None = Query(None)
    ) -> PushPrefsView:
        tenant = tenant_id or default_tenant
        if body.categories is not None:
            await prefs.set_categories(
                tenant,
                {k: ChannelPrefs(push=v.push, center=v.center) for k, v in body.categories.items()},
            )
        wants_quiet_update = (
            body.quiet_hours_enabled is not None
            or body.quiet_hours_start is not None
            or body.quiet_hours_end is not None
        )
        if wants_quiet_update:
            current = await prefs.get(tenant)
            try:
                await prefs.set_quiet_hours(
                    tenant,
                    enabled=(
                        body.quiet_hours_enabled
                        if body.quiet_hours_enabled is not None
                        else current.quiet_hours_enabled
                    ),
                    start=body.quiet_hours_start or current.quiet_hours_start,
                    end=body.quiet_hours_end or current.quiet_hours_end,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _prefs_view(await prefs.get(tenant))

    @router.post("/test", response_model=TestNotificationView)
    async def send_test_notification(
        body: TestNotificationRequest, tenant_id: str | None = Query(None)
    ) -> TestNotificationView:
        """Send one real push through the full pipeline — for manual end-to-end verification."""
        tenant = tenant_id or default_tenant
        result = await service.notify(
            tenant,
            category=body.category,
            title="Test notification",
            body="If you can see this, push notifications are working.",
            deep_link="/",
        )
        return TestNotificationView(
            outcome=result.outcome, sent_count=result.sent_count, pruned_count=result.pruned_count
        )

    return router
