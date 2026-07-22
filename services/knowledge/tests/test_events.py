"""The knowledge spine emitter (#665): debounce, batch vault sync, rate-limited failures.

The fake bus replaces only the wire — every test still goes through the real
``emit_event``, so envelope validation (module-prefixed type, payload cap, the
credential-shaped-key screen) is exercised on every emission. Due-ness is driven
through ``flush_due`` with a hand-rolled clock (the ADR-0092/0098 idiom).
"""

from __future__ import annotations

from typing import Any

from epicurus_knowledge.events import KnowledgeEventEmitter
from epicurus_knowledge.refs import KNOWLEDGE_KIND, SOURCE_NOTE, decode_ref

TENANT = "test"


class _FakeBus:
    """Records spine publishes — the repo-standard EventBus stand-in."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], str | None]] = []

    async def publish(
        self, subject: str, data: dict[str, Any], tenant_id: str | None = None
    ) -> None:
        self.published.append((subject, data, tenant_id))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _emitter(
    bus: _FakeBus,
    clock: _Clock,
    *,
    debounce_s: float = 120.0,
    cooldown_s: float = 900.0,
) -> KnowledgeEventEmitter:
    return KnowledgeEventEmitter(
        bus,  # type: ignore[arg-type]
        tenant=TENANT,
        debounce_s=debounce_s,
        failure_cooldown_s=cooldown_s,
        clock=clock,
    )


async def test_rapid_saves_settle_into_exactly_one_update() -> None:
    """The #665 acceptance: a burst of auto-saves is one event, after the quiet window."""
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)

    for _ in range(4):
        await emitter.doc_saved("kb/topic.md", created=False)
        clock.now += 3.0

    assert await emitter.flush_due() == 0
    assert bus.published == []

    clock.now += 120.0
    assert await emitter.flush_due() == 1
    [(subject, data, tenant)] = bus.published
    assert subject == "events.knowledge.doc_updated"
    assert tenant == TENANT
    assert data["payload"]["path"] == "kb/topic.md"
    assert data["payload"]["saves"] == 4
    # The ref is the resolvable shape knowledge_search cites: (source, path) round-trips.
    assert data["entity_ref"]["kind"] == KNOWLEDGE_KIND
    assert decode_ref(data["entity_ref"]["ref_id"]) == (SOURCE_NOTE, "kb/topic.md")


async def test_created_emits_immediately() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)
    await emitter.doc_saved("kb/new.md", created=True)
    [(subject, data, _)] = bus.published
    assert subject == "events.knowledge.doc_created"
    assert data["payload"]["path"] == "kb/new.md"


async def test_delete_drops_the_pending_update() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)
    await emitter.doc_saved("kb/doomed.md", created=False)
    await emitter.doc_deleted("kb/doomed.md")
    clock.now += 1000.0
    assert await emitter.flush_due() == 0
    [(subject, _, _)] = bus.published
    assert subject == "events.knowledge.doc_deleted"


async def test_folder_move_rekeys_pending_updates_under_the_prefix() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)
    await emitter.doc_saved("old/a.md", created=False)
    await emitter.doc_saved("old/deep/b.md", created=False)
    await emitter.doc_saved("other/c.md", created=False)

    emitter.doc_moved("old", "new")
    clock.now += 1000.0
    assert await emitter.flush_due() == 3
    paths = {d["payload"]["path"] for _, d, _ in bus.published}
    assert paths == {"new/a.md", "new/deep/b.md", "other/c.md"}


async def test_drop_prefix_forgets_a_deleted_base() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)
    await emitter.doc_saved("gone/a.md", created=False)
    await emitter.doc_saved("kept/b.md", created=False)
    emitter.drop_prefix("gone/")
    clock.now += 1000.0
    assert await emitter.flush_due() == 1
    [(_, data, _)] = bus.published
    assert data["payload"]["path"] == "kept/b.md"


async def test_vault_sync_is_one_batch_event_with_counts() -> None:
    """The #665 acceptance: an N-file sync pass is one event, carrying the pass's counts."""
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)

    await emitter.vault_synced({"indexed": 7, "deleted": 2, "unchanged": 40})

    [(subject, data, _)] = bus.published
    assert subject == "events.knowledge.vault_synced"
    assert data["payload"] == {"indexed": 7, "deleted": 2, "unchanged": 40}


async def test_noop_sync_pass_is_silent() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock)
    await emitter.vault_synced({"indexed": 0, "deleted": 0, "unchanged": 40})
    assert bus.published == []


async def test_index_failed_is_rate_limited() -> None:
    bus, clock = _FakeBus(), _Clock()
    emitter = _emitter(bus, clock, cooldown_s=900.0)

    await emitter.index_failed("embed backend down")
    await emitter.index_failed("embed backend down")  # inside the cooldown — suppressed
    clock.now += 899.0
    await emitter.index_failed("still down")  # still inside
    clock.now += 2.0
    await emitter.index_failed("still down")  # cooldown cleared — a fresh observation

    subjects = [s for s, _, _ in bus.published]
    assert subjects == ["events.knowledge.index_failed"] * 2
    assert bus.published[0][1]["payload"]["error"] == "embed backend down"


async def test_no_bus_disables_emission() -> None:
    clock = _Clock()
    emitter = KnowledgeEventEmitter(None, tenant=TENANT, clock=clock)
    await emitter.doc_saved("a.md", created=True)
    await emitter.vault_synced({"indexed": 1, "deleted": 0, "unchanged": 0})
    await emitter.index_failed("x")
    clock.now += 1000.0
    assert await emitter.flush_due() == 0
