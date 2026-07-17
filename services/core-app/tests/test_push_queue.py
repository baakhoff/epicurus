"""Unit tests for PushQueueStore and PushDigestScheduler.tick()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.push.prefs import PushPrefsStore
from epicurus_core_app.push.queue import PushDigestScheduler, PushQueueStore, QueuedPush

TENANT = "t1"


async def _queue() -> PushQueueStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = PushQueueStore(engine)
    await store.init()
    return store


async def _prefs() -> PushPrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = PushPrefsStore(engine)
    await store.init()
    return store


# ── PushQueueStore ───────────────────────────────────────────────────────────────


async def test_enqueue_and_list_round_trips() -> None:
    queue = await _queue()
    await queue.enqueue(tenant=TENANT, category="mail", title="New mail", body="From Alice")
    items = await queue.list_for_tenant(TENANT)
    assert len(items) == 1
    assert items[0].category == "mail"
    assert items[0].title == "New mail"
    assert items[0].deep_link is None
    assert items[0].entity_ref is None


async def test_enqueue_round_trips_deep_link_and_entity_ref() -> None:
    queue = await _queue()
    ref = {"ref_id": "e1", "module": "mail", "kind": "thread", "title": "Hello"}
    await queue.enqueue(
        tenant=TENANT,
        category="mail",
        title="t",
        body="b",
        deep_link="/m/mail/e1",
        entity_ref=ref,
    )
    items = await queue.list_for_tenant(TENANT)
    assert items[0].deep_link == "/m/mail/e1"
    assert items[0].entity_ref == ref


async def test_list_for_tenant_is_ordered_oldest_first() -> None:
    queue = await _queue()
    await queue.enqueue(tenant=TENANT, category="mail", title="first", body="b")
    await queue.enqueue(tenant=TENANT, category="tasks", title="second", body="b")
    items = await queue.list_for_tenant(TENANT)
    assert [i.title for i in items] == ["first", "second"]


async def test_distinct_tenants_reports_only_tenants_with_queued_rows() -> None:
    queue = await _queue()
    await queue.enqueue(tenant="a", category="mail", title="t", body="b")
    await queue.enqueue(tenant="b", category="mail", title="t", body="b")
    assert set(await queue.distinct_tenants()) == {"a", "b"}


async def test_delete_for_tenant_clears_only_that_tenant() -> None:
    queue = await _queue()
    await queue.enqueue(tenant="a", category="mail", title="t", body="b")
    await queue.enqueue(tenant="b", category="mail", title="t", body="b")
    deleted = await queue.delete_for_tenant("a")
    assert deleted == 1
    assert await queue.list_for_tenant("a") == []
    assert len(await queue.list_for_tenant("b")) == 1


# ── PushDigestScheduler ──────────────────────────────────────────────────────────


class _FakeSender:
    """Records every digest send; can be made to fail once to test the leave-queued path."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, list[QueuedPush]]] = []
        self._fail_first = fail_first

    async def __call__(self, tenant: str, items: list[QueuedPush]) -> None:
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("push service exploded")
        self.calls.append((tenant, items))


async def _utc() -> str:
    return "UTC"


async def test_tick_flushes_a_tenant_with_quiet_hours_disabled() -> None:
    queue = await _queue()
    prefs = await _prefs()
    await queue.enqueue(tenant=TENANT, category="mail", title="t", body="b")
    sender = _FakeSender()
    scheduler = PushDigestScheduler(queue, prefs, sender, timezone=_utc)
    await scheduler.tick()
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == TENANT
    assert await queue.list_for_tenant(TENANT) == []  # flushed and cleared


async def test_tick_leaves_a_tenant_queued_while_still_inside_quiet_hours() -> None:
    queue = await _queue()
    prefs = await _prefs()
    # The scheduler resolves "now" in UTC (timezone=_utc below) — anchor the window on UTC
    # too, or this flakes on any machine whose local clock isn't UTC.
    now_utc = datetime.now(UTC)
    # A window that starts 1 minute ago and ends in a minute — guaranteed to include "now"
    # regardless of when the test runs, without freezing the clock.
    start = (now_utc - timedelta(minutes=1)).time()
    end = (now_utc + timedelta(minutes=1)).time()
    await prefs.set_quiet_hours(
        TENANT, enabled=True, start=start.strftime("%H:%M"), end=end.strftime("%H:%M")
    )
    await queue.enqueue(tenant=TENANT, category="mail", title="t", body="b")
    sender = _FakeSender()
    scheduler = PushDigestScheduler(queue, prefs, sender, timezone=_utc)
    await scheduler.tick()
    assert sender.calls == []
    assert len(await queue.list_for_tenant(TENANT)) == 1  # still queued


async def test_tick_only_touches_tenants_with_queued_rows() -> None:
    queue = await _queue()
    prefs = await _prefs()
    await queue.enqueue(tenant="has-items", category="mail", title="t", body="b")
    sender = _FakeSender()
    scheduler = PushDigestScheduler(queue, prefs, sender, timezone=_utc)
    await scheduler.tick()
    assert [c[0] for c in sender.calls] == ["has-items"]


async def test_tick_never_raises_on_a_bad_timezone() -> None:
    queue = await _queue()
    prefs = await _prefs()
    await queue.enqueue(tenant=TENANT, category="mail", title="t", body="b")

    async def _bad_tz() -> str:
        return "Not/AZone"

    scheduler = PushDigestScheduler(queue, prefs, _FakeSender(), timezone=_bad_tz)
    await scheduler.tick()  # falls back to UTC rather than raising


async def test_a_failed_send_leaves_the_queue_intact_for_the_next_tick() -> None:
    queue = await _queue()
    prefs = await _prefs()
    await queue.enqueue(tenant=TENANT, category="mail", title="t", body="b")
    sender = _FakeSender(fail_first=True)
    scheduler = PushDigestScheduler(queue, prefs, sender, timezone=_utc)
    await scheduler.tick()  # must not raise — one tenant's failure can't break the tick
    assert len(await queue.list_for_tenant(TENANT)) == 1  # not cleared on failure

    await scheduler.tick()  # retried on the next tick, this time succeeding
    assert len(sender.calls) == 1
    assert await queue.list_for_tenant(TENANT) == []
