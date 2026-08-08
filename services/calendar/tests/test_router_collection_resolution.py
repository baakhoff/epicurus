"""Read/write collection resolution — the missing-provider invariant (#814).

`CollectionRouter` used to handle a **missing provider** oppositely on write and read: a
write (`create_event`, `find_free_slots`) fell back to local; the read aggregate
(`list_events`) `continue`d past the same ref and never tried local at all. The trigger is a
*stale* `prefs.enabled`/`prefs.active` ref — most commonly left behind once the account
behind it was disconnected, since nothing prunes `enabled` when that happens (core-app's
`disconnect_collections` prunes it on a clean `DELETE /oauth/{provider}`, but that cleanup is
best-effort, and nothing revisits `enabled` for prefs that go stale any other way). The
result: `calendar_create_event` reported a genuine, persisted `Event` while the Calendar page
stayed empty — events silently invisible forever, not lost, just unreadable through the
aggregate read.

This is tasks' #795 one module over, with a smaller blast radius: `_search_refs` always
appends `_LOCAL_REF`, so the *single-event* paths (`get_event`/`update_event`/`delete_event`)
already walked past a dead ref and reached local. Only `list_events` had no such backstop —
`prefs.enabled or [_LOCAL_REF]` falls back when `enabled` is *empty*, never when a ref inside
it is dead. Those single-event paths are still covered below, because the fix routes them
through the same helper and they must keep working (and must now warn instead of skipping
silently).

These tests exercise the fix against the *real* local store (file-backed SQLite per test,
never in-memory `StaticPool` — AGENTS.md's #677 pitfall for a store touched by more than one
task/session) and a second, independent file-backed store standing in for a connected
external ("google") provider — a plain `LocalCalendarProvider` satisfies the
`CalendarProvider` ABC just as well as the real Google backend for the router-level routing
behavior, which is all that's under test here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import create_async_engine
from structlog.testing import capture_logs

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers import router as router_module
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_calendar.providers.local import LocalCalendarProvider
from epicurus_calendar.providers.router import CollectionRouter
from epicurus_core import CollectionPrefs, CollectionRef

TENANT = "t"

# A stale ref to a *disconnected* Google account — never present in `external` unless a test
# says otherwise. Its `collection` is a made-up Google calendar id.
GOOGLE_REF = CollectionRef(account="google", collection="work@group.calendar.google.com")

# A second, independently stale ref to an account that was never configured at all — used to
# prove "several enabled, one disconnected" doesn't depend on which slot is stale.
GHOST_REF = CollectionRef(account="ghost", collection="gone")

# A window every test's events fall inside, wide enough that nothing is clipped by accident.
WINDOW = DateTimeRange(
    start=datetime(2026, 6, 1, tzinfo=UTC), end=datetime(2026, 6, 30, tzinfo=UTC)
)


def _at(hour: int, *, day: int = 15) -> datetime:
    return datetime(2026, 6, day, hour, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _unconfigured_structlog() -> Iterator[None]:
    """Isolate this file's `capture_logs()` assertions from another test's global structlog
    config (mirrors `test_modules_registry.py`) — a prior `configure_logging()` call
    elsewhere in the same pytest process would otherwise pin structlog's filtering level for
    the rest of the run.
    """
    was_configured = structlog.is_configured()
    prev_config = structlog.get_config() if was_configured else None
    structlog.reset_defaults()
    yield
    if was_configured and prev_config is not None:
        structlog.configure(**prev_config)
    else:
        structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _fresh_router_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the router module's logger for a never-yet-used proxy, for every test here.

    structlog's `cache_logger_on_first_use` (on in production — `configure_logging` sets
    it) permanently caches a bound logger onto a `BoundLoggerLazyProxy` the moment any
    method is first called on it: `BoundLoggerLazyProxy.bind()` overwrites its *own*
    `self.bind` with a closure over that first-resolved logger, bypassing `_CONFIG`
    (hence `capture_logs()`/`reset_defaults()`) forever after — for that proxy instance,
    which is the one long-lived module-level `epicurus_calendar.providers.router.log`. Any
    earlier test in the same pytest process that boots the real app and exercises the router
    (e.g. `test_resolver_attachments.py`, which builds a real `TestClient` before this file
    ever runs) can trip that first call while `configure_logging()`'s cache flag is on,
    poisoning the shared proxy for the rest of the process — `capture_logs()` then silently
    sees nothing. A fresh proxy has never been bound, so it always resolves against whatever
    processors are live *right now*, making these assertions independent of what ran earlier.
    """
    monkeypatch.setattr(router_module, "log", structlog.get_logger("epicurus_calendar.router"))


class _FixedPrefs:
    """A `CollectionPrefsSource` returning a fixed `CollectionPrefs`, set up per test."""

    def __init__(self, prefs: CollectionPrefs) -> None:
        self._prefs = prefs

    async def get_collections(self) -> CollectionPrefs:
        return self._prefs


async def _file_backed_store(path: Path) -> AsyncIterator[LocalEventStore]:
    """File-backed SQLite `LocalEventStore`, default pooling, disposed on teardown (#677
    pitfall: in-memory `StaticPool` is unsafe the moment a store is touched from more than
    one task/session, which a `CollectionRouter` juggling local + an external stand-in easily
    is)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    store = LocalEventStore(engine)
    await store.init()
    yield store
    await engine.dispose()


@pytest.fixture()
async def local_store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async for store in _file_backed_store(tmp_path / "local.db"):
        yield store


@pytest.fixture()
async def google_store(tmp_path: Path) -> AsyncIterator[LocalEventStore]:
    async for store in _file_backed_store(tmp_path / "google.db"):
        yield store


def _router(
    local_store: LocalEventStore,
    *,
    external: dict[str, CalendarProvider] | None = None,
    prefs: CollectionPrefs | None = None,
) -> CollectionRouter:
    return CollectionRouter(
        local=LocalCalendarProvider(store=local_store),
        external=external or {},
        prefs=_FixedPrefs(prefs if prefs is not None else CollectionPrefs()),
    )


async def _create(router: CollectionRouter, title: str, **kwargs: object) -> Event:
    return await router.create_event(
        tenant_id=TENANT,
        title=title,
        start=_at(9),
        end=_at(10),
        **kwargs,  # type: ignore[arg-type]
    )


def _degradations(
    logs: list[MutableMapping[str, Any]],
) -> list[MutableMapping[str, Any]]:
    """The `_resolve_provider` degradation warnings in a `capture_logs()` capture."""
    return [e for e in logs if "degrading to local" in str(e.get("event", ""))]


# ── the missing invariant, across every prefs shape (#814) ─────────────────────────────


def _case_no_external() -> tuple[CollectionPrefs, bool]:
    """(prefs, connect_google) — nothing enabled at all; everything is local by default."""
    return CollectionPrefs(), False


def _case_one_enabled_connected() -> tuple[CollectionPrefs, bool]:
    return CollectionPrefs(enabled=[GOOGLE_REF]), True


def _case_one_enabled_disconnected() -> tuple[CollectionPrefs, bool]:
    """The reported bug: the sole enabled ref's account has since been disconnected."""
    return CollectionPrefs(enabled=[GOOGLE_REF]), False


def _case_several_enabled_one_disconnected() -> tuple[CollectionPrefs, bool]:
    return CollectionPrefs(enabled=[GHOST_REF, GOOGLE_REF]), True


def _case_active_set_and_connected() -> tuple[CollectionPrefs, bool]:
    return CollectionPrefs(enabled=[GOOGLE_REF], active=GOOGLE_REF), True


def _case_active_set_but_disconnected() -> tuple[CollectionPrefs, bool]:
    """`active` itself — not just `enabled` — can go stale the same way."""
    return CollectionPrefs(enabled=[GOOGLE_REF], active=GOOGLE_REF), False


def _case_active_null_uses_first_enabled() -> tuple[CollectionPrefs, bool]:
    return CollectionPrefs(enabled=[GOOGLE_REF, GHOST_REF], active=None), True


_CASES: dict[str, Callable[[], tuple[CollectionPrefs, bool]]] = {
    "no_external": _case_no_external,
    "one_enabled_connected": _case_one_enabled_connected,
    "one_enabled_disconnected": _case_one_enabled_disconnected,
    "several_enabled_one_disconnected": _case_several_enabled_one_disconnected,
    "active_set_and_connected": _case_active_set_and_connected,
    "active_set_but_disconnected": _case_active_set_but_disconnected,
    "active_null_uses_first_enabled": _case_active_null_uses_first_enabled,
}


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_create_then_list_round_trips_across_every_prefs_shape(
    case: str, local_store: LocalEventStore, google_store: LocalEventStore
) -> None:
    """An event written via `create_event` is returned by the very next `list_events` — no
    matter what `prefs.enabled`/`prefs.active` look like, including a ref whose account is no
    longer connected. Before #814, `one_enabled_disconnected` (and any shape whose *whole*
    enabled set is dead) failed this: the write landed in local but the aggregate read never
    looked there.
    """
    prefs, connect_google = _CASES[case]()
    external: dict[str, CalendarProvider] = (
        {"google": LocalCalendarProvider(store=google_store)} if connect_google else {}
    )
    router = _router(local_store, external=external, prefs=prefs)

    created = await _create(router, f"round trip — {case}")
    events = await router.list_events(tenant_id=TENANT, time_range=WINDOW)

    ids = {e.id for e in events}
    assert created.id in ids, (
        f"[{case}] create_event created {created.id!r} but list_events did not return it "
        f"(got {sorted(ids)}) — a write must never be invisible to the very next read"
    )


# ── router-level regression: stale enabled ref reads must return local, never empty ────


async def test_stale_enabled_ref_reads_return_local_events_never_empty(
    local_store: LocalEventStore,
) -> None:
    """`prefs.enabled = [<google ref>]` with no google provider configured must surface the
    operator's local events — never silently report an empty window while events exist.
    """
    seeded = await LocalCalendarProvider(store=local_store).create_event(
        tenant_id=TENANT, title="Pre-existing event", start=_at(9), end=_at(10)
    )

    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))
    events = await router.list_events(tenant_id=TENANT, time_range=WINDOW)

    assert [e.id for e in events] == [seeded.id]


async def test_multiple_disconnected_refs_collapse_to_one_local_read_not_duplicated(
    local_store: LocalEventStore,
) -> None:
    """Two independently stale refs degrading to local must not each independently read it
    and double-count the operator's local events in the aggregate (`_dedup_refs`)."""
    seeded = await LocalCalendarProvider(store=local_store).create_event(
        tenant_id=TENANT, title="Just one event", start=_at(9), end=_at(10)
    )

    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF, GHOST_REF]))
    events = await router.list_events(tenant_id=TENANT, time_range=WINDOW)

    assert [e.id for e in events] == [seeded.id]


async def test_stale_ref_alongside_explicit_local_still_reads_local_exactly_once(
    local_store: LocalEventStore,
) -> None:
    """A degraded ref resolves onto `local` — which may *already* be in `enabled` in its own
    right. `_dedup_refs` collapses the pair, so the aggregate reads local once, not twice."""
    seeded = await LocalCalendarProvider(store=local_store).create_event(
        tenant_id=TENANT, title="Counted once", start=_at(9), end=_at(10)
    )

    router = _router(
        local_store,
        prefs=CollectionPrefs(enabled=[GOOGLE_REF, CollectionRef(account="local")]),
    )
    events = await router.list_events(tenant_id=TENANT, time_range=WINDOW)

    assert [e.id for e in events] == [seeded.id]


async def test_degraded_read_tags_events_with_the_local_token_not_the_dead_calendar(
    local_store: LocalEventStore,
) -> None:
    """Events surfaced through a degraded ref carry `calendar_id="local"` — the token the
    page's per-calendar visibility toggles can actually match (#378). Tagging them with the
    disconnected calendar's token would put them back out of reach, in a different way.
    """
    await LocalCalendarProvider(store=local_store).create_event(
        tenant_id=TENANT, title="Tagged local", start=_at(9), end=_at(10)
    )

    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))
    events = await router.list_events(tenant_id=TENANT, time_range=WINDOW)

    assert [e.calendar_id for e in events] == ["local"]


# ── a degraded read source logs a warning naming the account + collection ──────────────


async def test_degraded_read_source_logs_a_warning_naming_account_and_collection(
    local_store: LocalEventStore,
) -> None:
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    with capture_logs() as logs:
        await router.list_events(tenant_id=TENANT, time_range=WINDOW)

    degraded = _degradations(logs)
    assert len(degraded) == 1, f"expected exactly one degradation warning, got {logs}"
    assert degraded[0]["log_level"] == "warning"
    assert degraded[0]["account"] == "google"
    assert degraded[0]["collection"] == GOOGLE_REF.collection


async def test_degraded_write_target_also_logs_a_warning(local_store: LocalEventStore) -> None:
    """The write side degrades through the same `_resolve_provider` rule, so it warns too —
    previously nothing logged at all on either path for a disconnected account.

    A *stated* target is what degrades: `active` explicitly names the calendar the operator
    picked, and `_active_ref` returns it as-is rather than second-guessing it (see the
    companion test below for the case where there is no stated target).
    """
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF], active=GOOGLE_REF))

    with capture_logs() as logs:
        await _create(router, "Quarterly review")

    degraded = _degradations(logs)
    assert len(degraded) == 1
    assert degraded[0]["account"] == "google"
    assert degraded[0]["collection"] == GOOGLE_REF.collection


async def test_write_with_no_active_picks_local_without_a_spurious_warning(
    local_store: LocalEventStore,
) -> None:
    """With `active` unset, `_active_ref` *chooses* a target rather than honouring a stated
    one — and its enabled-set scan already skips a ref whose provider is gone, so it arrives
    at `_LOCAL_REF` under its own steam and there is nothing to degrade. The write still lands
    in local; it just isn't a degradation, and must not be logged as one (this is where
    calendar differs from tasks' `_resolve_collection`, which takes `enabled[0]` blindly and
    therefore *does* degrade in this same shape).
    """
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF], active=None))

    with capture_logs() as logs:
        created = await _create(router, "Lands in local anyway")

    assert not _degradations(logs)
    assert (
        await LocalCalendarProvider(store=local_store).get_event(
            tenant_id=TENANT, event_id=created.id
        )
        is not None
    )


async def test_healthy_connected_source_emits_no_degradation_warning(
    local_store: LocalEventStore, google_store: LocalEventStore
) -> None:
    """No false-positive noise: a live, connected source must never log a degradation."""
    external: dict[str, CalendarProvider] = {"google": LocalCalendarProvider(store=google_store)}
    router = _router(local_store, external=external, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    with capture_logs() as logs:
        await _create(router, "Goes straight to google")
        await router.list_events(tenant_id=TENANT, time_range=WINDOW)

    assert not _degradations(logs)


# ── regression: the reported shape ──────────────────────────────────────────────────────


async def test_regression_two_creates_with_stale_google_ref_then_list_returns_both(
    local_store: LocalEventStore,
) -> None:
    """Two `calendar_create_event` calls succeed while a stale Google ref sits in `enabled`;
    the very next `list_events` must return both — not the empty Calendar page that the same
    bug produced for tasks in #795.
    """
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    first = await _create(router, "Dentist")
    second = await router.create_event(
        tenant_id=TENANT, title="Standup", start=_at(11), end=_at(12)
    )

    events = await router.list_events(tenant_id=TENANT, time_range=WINDOW)
    assert {e.id for e in events} == {first.id, second.id}


# ── single-event paths: still resolve, and no longer skip silently ─────────────────────


async def test_update_event_locates_an_event_written_under_a_stale_active_ref(
    local_store: LocalEventStore,
) -> None:
    """An event added while `active` held a stale ref lands in local (the write-side
    fallback); a later `update_event` with no calendar token — which searches
    active → enabled → local through the same `_dedup_refs` helper — must still find it."""
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF], active=GOOGLE_REF))
    created = await _create(router, "Rename me")

    updated = await router.update_event(tenant_id=TENANT, event_id=created.id, title="Renamed")

    assert updated is not None
    assert updated.title == "Renamed"


async def test_delete_event_locates_an_event_written_under_a_stale_active_ref(
    local_store: LocalEventStore,
) -> None:
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF], active=GOOGLE_REF))
    created = await _create(router, "Delete me")

    assert await router.delete_event(tenant_id=TENANT, event_id=created.id) is True
    assert await router.get_event(tenant_id=TENANT, event_id=created.id) is None


async def test_get_event_through_a_stale_ref_warns_instead_of_skipping_silently(
    local_store: LocalEventStore,
) -> None:
    """`_search_refs` always appends local, so `get_event` already *reached* local past a dead
    ref — but it did so with no log line at all. It now degrades through the shared rule, so
    the dropped source is visible in the logs, and local is still consulted exactly once."""
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))
    created = await _create(router, "Findable")

    with capture_logs() as logs:
        found = await router.get_event(tenant_id=TENANT, event_id=created.id)

    assert found is not None and found.id == created.id
    degraded = _degradations(logs)
    assert len(degraded) == 1
    assert degraded[0]["account"] == "google"


# ── the explicit-token write branch degrades too, rather than misrouting ───────────────


async def test_explicit_calendar_token_for_a_disconnected_account_degrades_to_local(
    local_store: LocalEventStore, google_store: LocalEventStore
) -> None:
    """A create form's `account:collection` token naming an account that is gone must degrade
    to local — never silently land in some *other* connected account's calendar. A live,
    unrelated `google` connection is wired in alongside so a misroute would have somewhere to
    go.

    This one passed before #814 too: calendar's `create_event` has no equivalent of tasks'
    "the sole external provider owns this unlisted id" heuristic (`_resolve_collection`), so
    there was nothing here to misroute *to*. What the fix changes is the ref the write is
    carried out under — `_LOCAL_REF` now, rather than the dead account's collection id being
    handed to the local provider (which ignores `calendar_id`, so it was inert). Pinned so
    the invariant survives a future local backend that does honour collections.
    """
    external: dict[str, CalendarProvider] = {"google": LocalCalendarProvider(store=google_store)}
    router = _router(local_store, external=external, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    created = await _create(router, "Targets the ghost calendar", calendar_id="ghost:gone")

    assert (
        await LocalCalendarProvider(store=local_store).get_event(
            tenant_id=TENANT, event_id=created.id
        )
        is not None
    )
    assert (
        await LocalCalendarProvider(store=google_store).list_events(
            tenant_id=TENANT, time_range=WINDOW
        )
        == []
    )


# ── free/busy resolves through the same rule as the write it precedes ─────────────────


async def test_find_free_slots_degrades_to_the_same_calendar_the_write_would_land_on(
    local_store: LocalEventStore,
) -> None:
    """`find_free_slots` shares the write path's target resolution, so with a stale `active`
    it must compute busy time against **local** — the calendar the create actually lands on —
    and therefore see the local event that create just made.

    Also green pre-#814 (the old `or self._local` reached the same provider); it is here to
    keep free/busy and the write it precedes pinned to one resolution rule, since they now
    share the helper and could otherwise drift apart again.
    """
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF], active=GOOGLE_REF))
    await _create(router, "Blocks 09:00-10:00")

    slots = await router.find_free_slots(
        tenant_id=TENANT,
        time_range=DateTimeRange(start=_at(9), end=_at(11)),
        duration_minutes=60,
    )

    assert slots, "expected the 10:00-11:00 hour to be free"
    assert all(slot.start >= _at(9) + timedelta(hours=1) for slot in slots), (
        f"free/busy ignored the local event the write landed on — got {slots}"
    )
