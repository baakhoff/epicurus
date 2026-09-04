"""Integration test: new mail reaches a subscriber with the Mail page never opened. Requires Docker.

The reported symptom behind #796 was "my plan scheduled on this event does nothing". Every unit
test in this suite proves the poller *emits* — against a fake bus, which will happily accept a
subject a real broker would route somewhere else entirely. What only a real server can prove is
the rest of the sentence: that the event the background loop publishes lands on the subject the
core's intake is actually listening to, and therefore reaches an automation.

So this subscribes with the intake's own cross-tenant wildcard (``*.events.>``, see
``EventIntake.start``) rather than importing core-app — a module must not depend on the core's
package — and asserts a subscriber standing in for the automation matcher receives the event.
The mailbox page is never called; there is no HTTP client in this file at all.

The cache is **file-backed** SQLite, not in-memory + ``StaticPool``: the poller writes from its
own task while the test waits on the NATS callback task, and ``StaticPool`` shares one DBAPI
connection across every session, so a checkout-return ``ROLLBACK`` can land inside a concurrent
writer's transaction and silently erase it. A file database gives each session its own
connection, which is what production Postgres does anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core import Event, EventBus, EventEnvelope
from epicurus_mail.cache import CachedMailbox
from epicurus_mail.db import MailCache
from epicurus_mail.poller import run_periodic
from epicurus_mail.provider import (
    MailAvailability,
    MailCursor,
    MailLabel,
    MailMessage,
    MailProvider,
    MailThreadSummary,
    ThreadChanges,
    ThreadPage,
)

pytestmark = pytest.mark.integration

TENANT = "local"


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[MailCache]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail-cache.db'}")
    store = MailCache(engine)
    await store.init()
    yield store
    # Dispose before the test's loop closes: each aiosqlite connection owns a worker thread,
    # and an undisposed engine leaves them raising "Event loop is closed" at GC.
    await engine.dispose()


def _provider_with_new_mail() -> AsyncMock:
    """A mailbox whose next delta reports one genuinely-new message, then goes quiet."""
    summary = MailThreadSummary(
        id="t1", subject="s-t1", sender="a@x.com", snippet="snip", date="", label_ids=["INBOX"]
    )
    provider = AsyncMock(spec=MailProvider)
    provider.availability = AsyncMock(return_value=MailAvailability(state="connected"))
    provider.current_cursor = AsyncMock(return_value=MailCursor(history_id=100))
    provider.list_labels = AsyncMock(return_value=[MailLabel(id="INBOX", title="Inbox", unread=1)])
    provider.list_threads = AsyncMock(return_value=ThreadPage(threads=[summary]))
    provider.get_thread_summary = AsyncMock(return_value=summary)
    provider.messages_since = AsyncMock(return_value=[])
    provider.read = AsyncMock(
        return_value=MailMessage(
            id="m1",
            thread_id="t1",
            subject="Invoice from Acme",
            sender="acme@example.com",
            to=["me@example.com"],
            date="",
            snippet="Please find attached…",
            body="the full body — must never reach an event payload",
            label_ids=["INBOX"],
        )
    )

    async def _changed(cursor: MailCursor) -> ThreadChanges:
        if (cursor.history_id or 0) < 101:
            return ThreadChanges(
                changed_thread_ids={"t1"},
                new_message_ids={"m1"},
                next_cursor=MailCursor(history_id=101),
            )
        return ThreadChanges(next_cursor=cursor)

    provider.changed_threads_since = AsyncMock(side_effect=_changed)
    return provider


async def test_new_mail_reaches_a_subscriber_with_the_page_never_opened(
    nats_url: str, cache: MailCache
) -> None:
    """emit → NATS → a ``mail.received`` subscriber, driven only by the background loop (#796).

    This is the chain an automation on "new mail arrived" rides. Before the poller existed it
    could only ever start from a human opening Mail, which is why an unattended plan never ran.
    """
    provider = _provider_with_new_mail()
    delivered: asyncio.Queue[EventEnvelope] = asyncio.Queue()

    async with EventBus(nats_url) as bus:

        async def _automation(event: Event) -> None:
            """Stands in for the core's intake → automation matcher, wildcard and all."""
            envelope = EventEnvelope.model_validate(event.json())
            if envelope.type == "mail.received":
                await delivered.put(envelope)

        await bus.subscribe_any_tenant("events.>", _automation)
        await bus.client.flush()

        # A mailbox that has been synced before, so the tick takes the incremental delta path.
        await cache.set_cursor(tenant_id=TENANT, cursor=MailCursor(history_id=100))
        mailbox = CachedMailbox(provider, cache, tenant_id=TENANT, bus=bus, provider_name="gmail")

        poller = asyncio.create_task(
            run_periodic(mailbox=mailbox, provider=provider, tenant=TENANT, poll_interval_s=0.05)
        )
        try:
            envelope = await asyncio.wait_for(delivered.get(), timeout=20)
        finally:
            poller.cancel()
            with suppress(asyncio.CancelledError):
                await poller

    assert envelope.tenant_id == TENANT  # tenant-scoped end to end (constraint #1)
    assert envelope.module == "mail"
    assert envelope.dedup_key == "m1"
    assert envelope.payload["message_id"] == "m1"
    assert envelope.payload["subject"] == "Invoice from Acme"
    assert envelope.payload["folder"] == "INBOX"
    assert "body" not in envelope.payload  # pointers and metadata only, never content
    # The page was never read: no full-sync/landing fetch ever happened.
    provider.list_threads.assert_not_awaited()
