"""Read/write collection resolution — the missing-provider invariant (#795).

`TasksRouter` used to handle a **missing provider** oppositely on write and read: a write
(`_resolve_collection`) fell back to local; the read aggregate (`list_tasks`) `continue`d
past the same ref and never tried local at all. The trigger is a *stale* `prefs.enabled`/
`prefs.active` ref — most commonly left behind once the account behind it was disconnected,
since nothing prunes `enabled` when that happens (core-app's `disconnect_collections` prunes
it on a clean `DELETE /oauth/{provider}`, but that cleanup is best-effort, and nothing
revisits `enabled` for prefs that go stale any other way). The result: `tasks_add` reported a
genuine, persisted `Task` while the very next `tasks_list`/board read came back empty — tasks
silently invisible forever, not lost, just unreadable through the router.

These tests exercise the fix against the *real* local store (file-backed SQLite per test,
never in-memory `StaticPool` — AGENTS.md's #677 pitfall for a store touched by more than one
task/session) and a second, independent file-backed store standing in for a connected
external ("google") provider — a plain `LocalTasksProvider` satisfies the `TasksProvider`
Protocol just as well as the real Google backend for router-level routing behavior, which is
all that's under test here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import create_async_engine
from structlog.testing import capture_logs

from epicurus_core import CollectionPrefs, CollectionRef
from epicurus_tasks import router as router_module
from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task
from epicurus_tasks.providers import TasksProvider
from epicurus_tasks.router import TasksRouter

TENANT = "t"

# A stale ref to a *disconnected* Google account — never present in `external` unless a test
# says otherwise. Its `collection` is a made-up Google task-list id.
GOOGLE_REF = CollectionRef(account="google", collection="work")

# A second, independently stale ref to an account that was never configured at all — used to
# prove "several enabled, one disconnected" doesn't depend on which slot is stale.
GHOST_REF = CollectionRef(account="ghost", collection="gone")


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
    which is the one long-lived module-level `epicurus_tasks.router.log`. Any earlier test
    in the same pytest process that boots the real app and exercises the router (e.g.
    `test_resolver_attachments.py`, which builds a real `TestClient` before this file ever
    runs) can trip that first call while `configure_logging()`'s cache flag is on, poisoning
    the shared proxy for the rest of the process — `capture_logs()` then silently sees
    nothing (empirically confirmed: these tests pass in isolation, fail in the full suite).
    A fresh proxy has never been bound, so it always resolves against whatever processors
    are live *right now*, making these assertions independent of what ran earlier.
    """
    monkeypatch.setattr(router_module, "log", structlog.get_logger("epicurus_tasks.router"))


class _FixedPrefs:
    """A `CollectionPrefsSource` returning a fixed `CollectionPrefs`, set up per test."""

    def __init__(self, prefs: CollectionPrefs) -> None:
        self._prefs = prefs

    async def get_collections(self) -> CollectionPrefs:
        return self._prefs


async def _file_backed_store(path: Path) -> AsyncIterator[TaskStore]:
    """File-backed SQLite `TaskStore`, default pooling, disposed on teardown (#677 pitfall:
    in-memory `StaticPool` is unsafe the moment a store is touched from more than one
    task/session, which a `TasksRouter` juggling local + an external stand-in easily is)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    store = TaskStore(engine)
    await store.init()
    yield store
    await engine.dispose()


@pytest.fixture()
async def local_store(tmp_path: Path) -> AsyncIterator[TaskStore]:
    async for store in _file_backed_store(tmp_path / "local.db"):
        yield store


@pytest.fixture()
async def google_store(tmp_path: Path) -> AsyncIterator[TaskStore]:
    async for store in _file_backed_store(tmp_path / "google.db"):
        yield store


def _router(
    local_store: TaskStore,
    *,
    external: dict[str, TasksProvider] | None = None,
    prefs: CollectionPrefs | None = None,
) -> TasksRouter:
    return TasksRouter(
        local=LocalTasksProvider(local_store),
        external=external or {},
        prefs=_FixedPrefs(prefs if prefs is not None else CollectionPrefs()),
    )


# ── the missing invariant, across every prefs shape (#795) ─────────────────────────────


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
async def test_add_then_list_round_trips_across_every_prefs_shape(
    case: str, local_store: TaskStore, google_store: TaskStore
) -> None:
    """A task written via `add_task` is returned by the very next `list_tasks` — no matter
    what `prefs.enabled`/`prefs.active` look like, including a ref whose account is no
    longer connected. Before #795, `one_enabled_disconnected` (and any shape containing a
    disconnected ref) failed this: the write landed in local but the read never looked
    there.
    """
    prefs, connect_google = _CASES[case]()
    external: dict[str, TasksProvider] = (
        {"google": LocalTasksProvider(google_store)} if connect_google else {}
    )
    router = _router(local_store, external=external, prefs=prefs)

    added = await router.add_task(TENANT, f"round trip — {case}")
    tasks = await router.list_tasks(TENANT, scope="all")

    ids = {t.id for t in tasks}
    assert added.id in ids, (
        f"[{case}] add_task created {added.id!r} but list_tasks did not return it "
        f"(got {sorted(ids)}) — a write must never be invisible to the very next read"
    )


# ── router-level regression: stale enabled ref reads must return local, never empty ────


async def test_stale_enabled_ref_reads_return_local_tasks_never_empty(
    local_store: TaskStore,
) -> None:
    """`prefs.enabled = [<google ref>]` with no google provider configured must surface the
    operator's local tasks — never silently report an empty list while tasks exist.
    """
    seeded: Task = await LocalTasksProvider(local_store).add_task(TENANT, "Pre-existing task")

    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))
    tasks = await router.list_tasks(TENANT, scope="all")

    assert [t.id for t in tasks] == [seeded.id]


async def test_multiple_disconnected_refs_collapse_to_one_local_read_not_duplicated(
    local_store: TaskStore,
) -> None:
    """Two independently stale refs degrading to local must not each independently read it
    and double-count the operator's local tasks in the aggregate (`_dedup_refs`)."""
    seeded = await LocalTasksProvider(local_store).add_task(TENANT, "Just one task")

    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF, GHOST_REF]))
    tasks = await router.list_tasks(TENANT, scope="all")

    assert [t.id for t in tasks] == [seeded.id]


# ── a degraded read source logs a warning naming the account + collection ──────────────


async def test_degraded_read_source_logs_a_warning_naming_account_and_collection(
    local_store: TaskStore,
) -> None:
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    with capture_logs() as logs:
        await router.list_tasks(TENANT, scope="all")

    degraded = [e for e in logs if "degrading to local" in str(e.get("event", ""))]
    assert len(degraded) == 1, f"expected exactly one degradation warning, got {logs}"
    assert degraded[0]["log_level"] == "warning"
    assert degraded[0]["account"] == "google"
    assert degraded[0]["collection"] == "work"


async def test_degraded_write_target_also_logs_a_warning(local_store: TaskStore) -> None:
    """The write side degrades through the same `_resolve_provider` rule, so it warns too —
    previously nothing logged at all on either path for a disconnected account."""
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    with capture_logs() as logs:
        await router.add_task(TENANT, "Pay Perplexity AI $20 invoice")

    degraded = [e for e in logs if "degrading to local" in str(e.get("event", ""))]
    assert len(degraded) == 1
    assert degraded[0]["account"] == "google"


async def test_healthy_connected_source_emits_no_degradation_warning(
    local_store: TaskStore, google_store: TaskStore
) -> None:
    """No false-positive noise: a live, connected source must never log a degradation."""
    external: dict[str, TasksProvider] = {"google": LocalTasksProvider(google_store)}
    router = _router(local_store, external=external, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    with capture_logs() as logs:
        await router.add_task(TENANT, "Goes straight to google")
        await router.list_tasks(TENANT, scope="all")

    assert not [e for e in logs if "degrading to local" in str(e.get("event", ""))]


# ── regression: the exact reported turn ─────────────────────────────────────────────────


async def test_regression_two_adds_with_stale_google_ref_then_list_returns_both(
    local_store: TaskStore,
) -> None:
    """The reported turn (#795): two `tasks_add` calls succeed (green in the timeline) while
    a stale Google ref sits in `enabled`; the very next list read must return both — not the
    empty Tasks page that was actually observed.
    """
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF]))

    first = await router.add_task(TENANT, "Pay Perplexity AI $20 invoice")
    second = await router.add_task(
        TENANT, "Handle GoDaddy domain renewal/cancellation for baakhofficial.com"
    )

    tasks = await router.list_tasks(TENANT, scope="all")
    assert {t.id for t in tasks} == {first.id, second.id}


# ── mutation search still locates a task written under a stale ref ─────────────────────


async def test_complete_task_locates_a_task_written_under_a_stale_active_ref(
    local_store: TaskStore,
) -> None:
    """A task added while `active` held a stale ref lands in local (the write-side fallback);
    a later mutation with no `list_id` — which searches active → enabled → local via the same
    `_dedup_refs` helper — must still find and complete it."""
    router = _router(local_store, prefs=CollectionPrefs(enabled=[GOOGLE_REF], active=GOOGLE_REF))
    added = await router.add_task(TENANT, "Complete me")

    done = await router.complete_task(TENANT, added.id)

    assert done.completed


# ── closes the related (#795-adjacent) explicit list_id asymmetry, low risk (item 5) ───


async def test_explicit_list_id_matching_a_disconnected_ref_degrades_to_local(
    local_store: TaskStore, google_store: TaskStore
) -> None:
    """An explicit `list_id` that matches an *enabled* ref whose own provider is gone must
    degrade to local — not fall through to "the sole external provider owns this unlisted
    id" and silently write into a different, unrelated account's list. A live, unrelated
    `google` connection is wired in alongside the stale `ghost` ref so that heuristic has a
    candidate to (incorrectly, pre-fix) claim the id.
    """
    external: dict[str, TasksProvider] = {"google": LocalTasksProvider(google_store)}
    router = _router(local_store, external=external, prefs=CollectionPrefs(enabled=[GHOST_REF]))

    added = await router.add_task(TENANT, "Targets the ghost list", list_id=GHOST_REF.collection)

    assert await LocalTasksProvider(local_store).get_task(TENANT, added.id) is not None
    assert await LocalTasksProvider(google_store).list_tasks(TENANT, scope="all") == []
