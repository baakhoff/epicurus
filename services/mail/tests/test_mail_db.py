"""Unit tests for the tenant-scoped mail cache store (ADR-0096, #623).

Exercised on an in-memory SQLite engine with a StaticPool (one shared connection), the
same pattern the tasks store uses — so the schema built by ``init()`` is the one the store
reads back. Includes a large-int round-trip that guards the ``BigInteger`` mapping: a wrong
``Integer`` column would pass on SQLite but overflow on Postgres.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_mail.db import MailCache
from epicurus_mail.provider import MailCursor, MailLabel, MailThreadSummary

TENANT = "local"
OTHER = "tenant-2"


def _engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _cache() -> MailCache:
    cache = MailCache(_engine())
    await cache.init()
    return cache


def _summary(tid: str, *, unread: bool = False, sort_ts: int = 0) -> MailThreadSummary:
    return MailThreadSummary(
        id=tid,
        subject=f"subject-{tid}",
        sender="a@x.com",
        snippet="snip",
        date="Mon, 1 Jan 2024 10:00:00 +0000",
        unread=unread,
        message_count=1,
        sort_ts=sort_ts,
    )


async def test_landing_replace_get_orders_by_sort_ts_desc() -> None:
    cache = await _cache()
    await cache.replace_landing(
        tenant_id=TENANT,
        label="INBOX",
        threads=[
            _summary("old", sort_ts=100),
            _summary("new", sort_ts=300),
            _summary("mid", sort_ts=200),
        ],
        next_cursor="NEXT",
    )
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert [r.id for r in rows] == ["new", "mid", "old"]  # newest (largest sort_ts) first
    assert await cache.get_landing_cursor(tenant_id=TENANT, label="INBOX") == "NEXT"


async def test_has_landing_reflects_presence() -> None:
    cache = await _cache()
    assert await cache.has_landing(tenant_id=TENANT, label="INBOX") is False
    await cache.replace_landing(
        tenant_id=TENANT, label="INBOX", threads=[_summary("t1")], next_cursor=None
    )
    assert await cache.has_landing(tenant_id=TENANT, label="INBOX") is True
    # A different label / tenant is a separate cache slot.
    assert await cache.has_landing(tenant_id=TENANT, label="SENT") is False
    assert await cache.has_landing(tenant_id=OTHER, label="INBOX") is False


async def test_replace_landing_swaps_the_page() -> None:
    cache = await _cache()
    await cache.replace_landing(
        tenant_id=TENANT, label="INBOX", threads=[_summary("a"), _summary("b")], next_cursor=None
    )
    await cache.replace_landing(
        tenant_id=TENANT, label="INBOX", threads=[_summary("c")], next_cursor=None
    )
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert [r.id for r in rows] == ["c"]  # old rows gone


async def test_upsert_thread_row_inserts_and_refreshes() -> None:
    cache = await _cache()
    await cache.upsert_thread_row(
        tenant_id=TENANT, label="INBOX", summary=_summary("t1", unread=True, sort_ts=100)
    )
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert rows[0].unread is True
    # Re-upsert with a newer sort_ts + read flag — no duplicate row, values refreshed.
    await cache.upsert_thread_row(
        tenant_id=TENANT, label="INBOX", summary=_summary("t1", unread=False, sort_ts=500)
    )
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert len(rows) == 1
    assert rows[0].unread is False
    assert rows[0].sort_ts == 500


async def test_remove_thread_from_label_vs_everywhere() -> None:
    cache = await _cache()
    for label in ("INBOX", "IMPORTANT"):
        await cache.upsert_thread_row(tenant_id=TENANT, label=label, summary=_summary("t1"))
    await cache.remove_thread_from_label(tenant_id=TENANT, label="INBOX", thread_id="t1")
    assert await cache.has_landing(tenant_id=TENANT, label="INBOX") is False
    assert await cache.has_landing(tenant_id=TENANT, label="IMPORTANT") is True  # still filed here
    # remove_thread drops it from every folder.
    await cache.remove_thread(tenant_id=TENANT, thread_id="t1")
    assert await cache.has_landing(tenant_id=TENANT, label="IMPORTANT") is False


async def test_set_thread_unread_flips_across_all_labels() -> None:
    cache = await _cache()
    for label in ("INBOX", "IMPORTANT"):
        await cache.upsert_thread_row(
            tenant_id=TENANT, label=label, summary=_summary("t1", unread=True)
        )
    await cache.set_thread_unread(tenant_id=TENANT, thread_id="t1", unread=False)
    for label in ("INBOX", "IMPORTANT"):
        rows = await cache.get_landing(tenant_id=TENANT, label=label, limit=25)
        assert rows[0].unread is False


async def test_prune_landing_keeps_newest() -> None:
    cache = await _cache()
    await cache.replace_landing(
        tenant_id=TENANT,
        label="INBOX",
        threads=[_summary(f"t{i}", sort_ts=i) for i in range(10)],
        next_cursor=None,
    )
    await cache.prune_landing(tenant_id=TENANT, label="INBOX", keep=3)
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert [r.id for r in rows] == ["t9", "t8", "t7"]  # only the three newest survive


async def test_labels_replace_and_get_preserve_order() -> None:
    cache = await _cache()
    await cache.replace_labels(
        tenant_id=TENANT,
        labels=[
            MailLabel(id="INBOX", title="Inbox", kind="system", unread=5),
            MailLabel(id="Work", title="Work", kind="user", unread=None),
        ],
    )
    labels = await cache.get_labels(tenant_id=TENANT)
    assert [lbl.id for lbl in labels] == ["INBOX", "Work"]  # stored order preserved
    assert labels[0].unread == 5
    assert labels[1].unread is None  # capability-gated count stays None, not 0


async def test_cursor_roundtrip_survives_large_history_id() -> None:
    """A ``historyId`` and ``sort_ts`` beyond int32 round-trip intact (the BigInteger guard).

    ``9_876_543_210`` > 2**31 (2_147_483_647): an ``Integer`` column would overflow on
    Postgres. SQLite tolerates it, so this test's value proves the *mapping* is BigInteger by
    reading the same number back, catching a regression that Postgres would otherwise expose
    only in production.
    """
    cache = await _cache()
    big = 9_876_543_210  # > int32
    assert (await cache.get_cursor(tenant_id=TENANT)).is_empty()  # cold
    await cache.set_cursor(tenant_id=TENANT, cursor=MailCursor(history_id=big))
    restored = await cache.get_cursor(tenant_id=TENANT)
    assert restored.history_id == big
    # set_cursor upserts (no duplicate rows), and IMAP fields round-trip too.
    await cache.set_cursor(
        tenant_id=TENANT, cursor=MailCursor(uid_validity=big + 1, uid_next=big + 2)
    )
    restored = await cache.get_cursor(tenant_id=TENANT)
    assert restored.history_id is None
    assert restored.uid_validity == big + 1
    assert restored.uid_next == big + 2


async def test_big_sort_ts_roundtrips() -> None:
    """An epoch-millisecond ``sort_ts`` (~1.75e12) round-trips — BigInteger, not Integer."""
    cache = await _cache()
    ms = 1_752_000_000_000  # ~2025 in epoch ms, far beyond int32
    await cache.upsert_thread_row(
        tenant_id=TENANT, label="INBOX", summary=_summary("t1", sort_ts=ms)
    )
    rows = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    assert rows[0].sort_ts == ms


async def test_tenant_isolation() -> None:
    cache = await _cache()
    await cache.upsert_thread_row(tenant_id=TENANT, label="INBOX", summary=_summary("mine"))
    await cache.upsert_thread_row(tenant_id=OTHER, label="INBOX", summary=_summary("theirs"))
    mine = await cache.get_landing(tenant_id=TENANT, label="INBOX", limit=25)
    theirs = await cache.get_landing(tenant_id=OTHER, label="INBOX", limit=25)
    assert [r.id for r in mine] == ["mine"]
    assert [r.id for r in theirs] == ["theirs"]


async def test_init_is_idempotent() -> None:
    """init() runs create_all + the additive reconcile; running twice is a harmless no-op."""
    cache = await _cache()
    await cache.init()  # second run must not raise
    assert (await cache.get_cursor(tenant_id=TENANT)).is_empty()
