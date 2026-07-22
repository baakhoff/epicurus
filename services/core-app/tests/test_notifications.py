"""Unit tests for NotificationStore: CRUD, read-state, retention pruning."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.notifications import NotificationStore

TENANT = "t1"


async def _store(max_per_tenant: int = 500) -> NotificationStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = NotificationStore(engine, max_per_tenant=max_per_tenant)
    await store.init()
    return store


# ── create / list ─────────────────────────────────────────────────────────────


async def test_create_returns_an_unread_notification() -> None:
    store = await _store()
    n = await store.create(tenant=TENANT, category="mail", title="New mail", body="From Alice")
    assert n.id and len(n.id) == 32  # uuid4 hex
    assert n.category == "mail"
    assert n.read_at is None


async def test_create_round_trips_entity_ref_deep_link_and_automation_id() -> None:
    store = await _store()
    ref = {"ref_id": "e1", "module": "mail", "kind": "thread", "title": "Hello"}
    n = await store.create(
        tenant=TENANT,
        category="mail",
        title="t",
        body="b",
        deep_link="/m/mail/e1",
        entity_ref=ref,
        automation_id="auto-1",
    )
    assert n.deep_link == "/m/mail/e1"
    assert n.entity_ref == ref
    assert n.automation_id == "auto-1"


async def test_create_without_optional_fields_defaults_to_none() -> None:
    store = await _store()
    n = await store.create(tenant=TENANT, category="mail", title="t", body="b")
    assert n.deep_link is None
    assert n.entity_ref is None
    assert n.automation_id is None


async def test_list_is_tenant_scoped_and_newest_first() -> None:
    store = await _store()
    await store.create(tenant=TENANT, category="mail", title="first", body="b")
    await store.create(tenant=TENANT, category="tasks", title="second", body="b")
    await store.create(tenant="other", category="mail", title="not mine", body="b")
    rows = await store.list(TENANT)
    assert [r.title for r in rows] == ["second", "first"]
    assert len(await store.list("other")) == 1


async def test_list_filters_by_category() -> None:
    store = await _store()
    await store.create(tenant=TENANT, category="mail", title="m", body="b")
    await store.create(tenant=TENANT, category="tasks", title="t", body="b")
    rows = await store.list(TENANT, category="mail")
    assert [r.title for r in rows] == ["m"]


async def test_list_filters_unread_only() -> None:
    store = await _store()
    a = await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant=TENANT, category="mail", title="b", body="b")
    await store.mark_read(tenant=TENANT, notification_id=a.id)
    rows = await store.list(TENANT, unread_only=True)
    assert [r.title for r in rows] == ["b"]


# ── unread_count / mark_read / mark_all_read ────────────────────────────────────


async def test_unread_count_reflects_read_state() -> None:
    store = await _store()
    a = await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant=TENANT, category="mail", title="b", body="b")
    assert await store.unread_count(TENANT) == 2
    await store.mark_read(tenant=TENANT, notification_id=a.id)
    assert await store.unread_count(TENANT) == 1


async def test_unread_count_is_tenant_scoped() -> None:
    store = await _store()
    await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant="other", category="mail", title="b", body="b")
    assert await store.unread_count(TENANT) == 1
    assert await store.unread_count("other") == 1
    assert await store.unread_count("nonexistent") == 0


async def test_mark_read_is_idempotent_and_reports_unknown() -> None:
    store = await _store()
    n = await store.create(tenant=TENANT, category="mail", title="a", body="b")
    assert await store.mark_read(tenant=TENANT, notification_id=n.id) is True
    assert await store.mark_read(tenant=TENANT, notification_id=n.id) is True  # already read
    assert await store.mark_read(tenant=TENANT, notification_id="nope") is False


async def test_mark_read_is_tenant_scoped() -> None:
    store = await _store()
    n = await store.create(tenant=TENANT, category="mail", title="a", body="b")
    assert await store.mark_read(tenant="other", notification_id=n.id) is False  # wrong tenant
    assert await store.unread_count(TENANT) == 1  # untouched


async def test_mark_all_read_marks_every_unread_row_and_returns_the_count() -> None:
    store = await _store()
    await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant=TENANT, category="tasks", title="b", body="b")
    marked = await store.mark_all_read(TENANT)
    assert marked == 2
    assert await store.unread_count(TENANT) == 0


async def test_mark_all_read_is_a_no_op_when_nothing_is_unread() -> None:
    store = await _store()
    n = await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.mark_read(tenant=TENANT, notification_id=n.id)
    assert await store.mark_all_read(TENANT) == 0


async def test_mark_all_read_is_tenant_scoped() -> None:
    store = await _store()
    await store.create(tenant=TENANT, category="mail", title="a", body="b")
    await store.create(tenant="other", category="mail", title="b", body="b")
    marked = await store.mark_all_read(TENANT)
    assert marked == 1
    assert await store.unread_count("other") == 1  # untouched


# ── retention pruning ─────────────────────────────────────────────────────────


async def test_create_prunes_the_oldest_row_past_the_cap() -> None:
    store = await _store(max_per_tenant=2)
    await store.create(tenant=TENANT, category="mail", title="oldest", body="b")
    await store.create(tenant=TENANT, category="mail", title="middle", body="b")
    await store.create(tenant=TENANT, category="mail", title="newest", body="b")
    rows = await store.list(TENANT)
    assert len(rows) == 2
    assert {r.title for r in rows} == {"middle", "newest"}  # "oldest" pruned


async def test_pruning_is_tenant_scoped() -> None:
    store = await _store(max_per_tenant=1)
    await store.create(tenant=TENANT, category="mail", title="mine", body="b")
    await store.create(tenant="other", category="mail", title="theirs", body="b")
    assert len(await store.list(TENANT)) == 1
    assert len(await store.list("other")) == 1  # a different tenant's cap, unaffected


async def test_no_pruning_below_the_cap() -> None:
    store = await _store(max_per_tenant=10)
    for i in range(5):
        await store.create(tenant=TENANT, category="mail", title=f"n{i}", body="b")
    assert len(await store.list(TENANT)) == 5
