"""Emulated recurrence on the Google provider (ADR-0082): the RepeatStore side table.

Google Tasks has no recurrence field, so a repeating Google task's RRULE lives in a module-owned
side table keyed by (tenant, list, task id). These tests mock the Google API at httpx and back the
provider with a real RepeatStore over in-memory SQLite, asserting the rule is persisted on write,
filled on read, returned on complete (so the router can materialize), and GC'd on delete / miss.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import PlatformClient
from epicurus_tasks.db import RepeatStore, TaskStore
from epicurus_tasks.google_provider import GoogleTasksProvider

TENANT = "test-tenant"
DEFAULT_LIST = "@default"


class _FakeResponse:
    def __init__(self, data: Any, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]


def _client(
    *,
    get: Any = None,
    post: Any = None,
    patch_: Any = None,
    delete: Any = None,
) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=get)
    client.post = AsyncMock(return_value=post)
    client.patch = AsyncMock(return_value=patch_)
    client.delete = AsyncMock(return_value=delete)
    return client


def _platform() -> PlatformClient:
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(return_value="ya29.mock")
    return platform  # type: ignore[return-value]


@pytest.fixture()
async def repeats() -> RepeatStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await TaskStore(engine).init()  # provisions task_repeats (shared _Base metadata)
    return RepeatStore(engine)


@pytest.fixture()
def provider(repeats: RepeatStore) -> GoogleTasksProvider:
    return GoogleTasksProvider(platform=_platform(), repeats=repeats)


def _mock(client: AsyncMock) -> Any:
    return patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=client)


async def test_add_task_persists_repeat_in_side_table(
    provider: GoogleTasksProvider, repeats: RepeatStore
) -> None:
    created = {"id": "g1", "title": "Water plants", "due": "2026-07-06T00:00:00.000Z"}
    with _mock(_client(post=_FakeResponse(created))):
        task = await provider.add_task(
            TENANT, "Water plants", due="2026-07-06", repeat="FREQ=WEEKLY"
        )
    assert task.repeat == "FREQ=WEEKLY"
    assert await repeats.get(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1") == "FREQ=WEEKLY"


async def test_add_task_without_repeat_writes_nothing(
    provider: GoogleTasksProvider, repeats: RepeatStore
) -> None:
    created = {"id": "g1", "title": "One-off"}
    with _mock(_client(post=_FakeResponse(created))):
        task = await provider.add_task(TENANT, "One-off")
    assert task.repeat is None
    assert await repeats.get(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1") is None


async def test_list_tasks_fills_repeat_from_side_table(
    provider: GoogleTasksProvider, repeats: RepeatStore
) -> None:
    await repeats.set(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1", rrule="FREQ=DAILY")
    items = {"items": [{"id": "g1", "title": "X", "status": "needsAction"}]}
    with _mock(_client(get=_FakeResponse(items))):
        tasks = await provider.list_tasks(TENANT)
    assert tasks[0].repeat == "FREQ=DAILY"


async def test_get_task_fills_repeat(provider: GoogleTasksProvider, repeats: RepeatStore) -> None:
    await repeats.set(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1", rrule="FREQ=MONTHLY")
    with _mock(_client(get=_FakeResponse({"id": "g1", "title": "X", "status": "needsAction"}))):
        task = await provider.get_task(TENANT, "g1")
    assert task is not None
    assert task.repeat == "FREQ=MONTHLY"


async def test_complete_task_returns_repeat_for_materialization(
    provider: GoogleTasksProvider, repeats: RepeatStore
) -> None:
    await repeats.set(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1", rrule="FREQ=WEEKLY")
    completed = {
        "id": "g1",
        "title": "X",
        "status": "completed",
        "completed": "2026-07-06T10:00:00Z",
    }
    with _mock(_client(patch_=_FakeResponse(completed))):
        done = await provider.complete_task(TENANT, "g1")
    assert done.completed
    assert done.repeat == "FREQ=WEEKLY"  # the router needs this to schedule the next instance


async def test_update_clears_repeat_with_empty_string(
    provider: GoogleTasksProvider, repeats: RepeatStore
) -> None:
    await repeats.set(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1", rrule="FREQ=DAILY")
    task_json = {"id": "g1", "title": "X", "status": "needsAction"}
    # repeat="" with no Google-mappable field → the provider GETs (empty body) then clears the rule.
    with _mock(_client(get=_FakeResponse(task_json))):
        task = await provider.update_task(TENANT, "g1", repeat="")
    assert task.repeat is None
    assert await repeats.get(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1") is None


async def test_delete_task_retires_the_rule(
    provider: GoogleTasksProvider, repeats: RepeatStore
) -> None:
    await repeats.set(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1", rrule="FREQ=DAILY")
    with _mock(_client(delete=_FakeResponse(None, status_code=204))):
        await provider.delete_task(TENANT, "g1")
    assert await repeats.get(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="g1") is None


async def test_get_task_404_gcs_the_orphaned_rule(
    provider: GoogleTasksProvider, repeats: RepeatStore
) -> None:
    """A task deleted in Google's own UI retires its rule on the next lookup (GC on miss)."""
    await repeats.set(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="gone", rrule="FREQ=DAILY")
    with _mock(_client(get=_FakeResponse(None, status_code=404))):
        result = await provider.get_task(TENANT, "gone")
    assert result is None
    assert await repeats.get(tenant_id=TENANT, list_id=DEFAULT_LIST, task_id="gone") is None


async def test_no_store_degrades_silently() -> None:
    """Without a RepeatStore (unit tests / misconfig) recurrence is a silent no-op, not an error."""
    provider = GoogleTasksProvider(platform=_platform())  # no repeats store
    created = {"id": "g1", "title": "X", "due": "2026-07-06T00:00:00.000Z"}
    with _mock(_client(post=_FakeResponse(created))):
        task = await provider.add_task(TENANT, "X", due="2026-07-06", repeat="FREQ=DAILY")
    assert task.repeat is None
