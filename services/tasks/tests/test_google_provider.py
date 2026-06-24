"""Tests for GoogleTasksProvider — the Google Tasks API is mocked at httpx; the
OAuth token comes from a mocked PlatformClient (no client secret in the module)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from epicurus_core import PlatformClient
from epicurus_tasks.google_provider import GoogleTasksError, GoogleTasksProvider

TENANT = "test-tenant"
MOCK_TOKEN = "ya29.mock-access-token"

_GOOGLE_TASK = {
    "id": "goog-task-1",
    "title": "Write tests",
    "notes": "Cover all edge cases",
    "due": "2025-06-15T00:00:00.000Z",
    "status": "needsAction",
}

_GOOGLE_TASK_COMPLETED = {
    "id": "goog-task-1",
    "title": "Write tests",
    "status": "completed",
    "completed": "2025-06-14T10:00:00.000Z",
}


class _FakeResponse:
    def __init__(self, data: Any, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )


def _tasks_client(tasks_resp: Any) -> AsyncMock:
    """A context-manager mock for the Google Tasks API httpx client."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=_FakeResponse(tasks_resp))
    client.post = AsyncMock(return_value=_FakeResponse(tasks_resp))
    client.patch = AsyncMock(return_value=_FakeResponse(tasks_resp))
    return client


def _make_platform(token: str = MOCK_TOKEN) -> PlatformClient:
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(return_value=token)
    return platform  # type: ignore[return-value]


@pytest.fixture()
def provider() -> GoogleTasksProvider:
    return GoogleTasksProvider(platform=_make_platform())


async def test_provider_name(provider: GoogleTasksProvider) -> None:
    assert provider.provider_name() == "google"


async def test_list_tasks(provider: GoogleTasksProvider) -> None:
    with patch(
        "epicurus_tasks.google_provider.httpx.AsyncClient",
        return_value=_tasks_client({"items": [_GOOGLE_TASK]}),
    ):
        tasks = await provider.list_tasks(TENANT)

    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "goog-task-1"
    assert t.title == "Write tests"
    assert t.notes == "Cover all edge cases"
    assert t.due == "2025-06-15"
    assert not t.completed


async def test_list_open_scope_asks_google_for_incomplete(provider: GoogleTasksProvider) -> None:
    """Default scope requests incomplete tasks only (ADR-0049)."""
    client = _tasks_client({"items": [_GOOGLE_TASK]})
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=client):
        await provider.list_tasks(TENANT)
    assert client.get.call_args.kwargs["params"] == {
        "showCompleted": "false",
        "showHidden": "false",
    }


async def test_list_done_scope_requests_and_filters_completed(
    provider: GoogleTasksProvider,
) -> None:
    """scope="done" asks Google for completed+hidden tasks and keeps only completed ones."""
    items = [
        {"id": "open-1", "title": "Open", "status": "needsAction"},
        {
            "id": "done-1",
            "title": "Done",
            "status": "completed",
            "completed": "2025-06-14T10:00:00Z",
        },
    ]
    client = _tasks_client({"items": items})
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=client):
        tasks = await provider.list_tasks(TENANT, scope="done")
    assert client.get.call_args.kwargs["params"] == {"showCompleted": "true", "showHidden": "true"}
    assert [t.id for t in tasks] == ["done-1"]
    assert tasks[0].completed


async def test_list_all_scope_keeps_open_and_completed(provider: GoogleTasksProvider) -> None:
    """scope="all" requests completed+hidden too and keeps everything."""
    items = [
        {"id": "open-1", "title": "Open", "status": "needsAction"},
        {"id": "done-1", "title": "Done", "status": "completed"},
    ]
    client = _tasks_client({"items": items})
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=client):
        tasks = await provider.list_tasks(TENANT, scope="all")
    assert client.get.call_args.kwargs["params"] == {"showCompleted": "true", "showHidden": "true"}
    assert {t.id for t in tasks} == {"open-1", "done-1"}


async def test_list_tasks_empty(provider: GoogleTasksProvider) -> None:
    with patch(
        "epicurus_tasks.google_provider.httpx.AsyncClient",
        return_value=_tasks_client({"items": []}),
    ):
        tasks = await provider.list_tasks(TENANT)
    assert tasks == []


async def test_list_tasks_missing_items_key(provider: GoogleTasksProvider) -> None:
    """Google omits 'items' when the list is empty."""
    with patch(
        "epicurus_tasks.google_provider.httpx.AsyncClient",
        return_value=_tasks_client({}),
    ):
        tasks = await provider.list_tasks(TENANT)
    assert tasks == []


async def test_add_task(provider: GoogleTasksProvider) -> None:
    new_task = {"id": "new-task-id", "title": "Ship the feature", "status": "needsAction"}
    with patch(
        "epicurus_tasks.google_provider.httpx.AsyncClient",
        return_value=_tasks_client(new_task),
    ):
        task = await provider.add_task(TENANT, "Ship the feature", due="2025-07-01")

    assert task.id == "new-task-id"
    assert task.title == "Ship the feature"


async def test_complete_task(provider: GoogleTasksProvider) -> None:
    with patch(
        "epicurus_tasks.google_provider.httpx.AsyncClient",
        return_value=_tasks_client(_GOOGLE_TASK_COMPLETED),
    ):
        task = await provider.complete_task(TENANT, "goog-task-1")

    assert task.completed
    assert task.completed_at == "2025-06-14T10:00:00.000Z"


async def test_not_connected_raises() -> None:
    """When the core reports the provider isn't connected, raise GoogleTasksError."""
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "404",
            request=None,  # type: ignore[arg-type]
            response=None,  # type: ignore[arg-type]
        )
    )
    provider = GoogleTasksProvider(platform=platform)  # type: ignore[arg-type]
    with pytest.raises(GoogleTasksError, match="not connected"):
        await provider.list_tasks(TENANT)


async def test_update_task(provider: GoogleTasksProvider) -> None:
    """Edit PATCHes only the supplied fields; due is sent as RFC 3339 midnight."""
    updated = {
        "id": "goog-task-1",
        "title": "Updated title",
        "notes": "new notes",
        "due": "2025-12-25T00:00:00.000Z",
        "status": "needsAction",
    }
    client = _tasks_client(updated)
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=client):
        task = await provider.update_task(
            TENANT, "goog-task-1", title="Updated title", notes="new notes", due="2025-12-25"
        )

    assert task.title == "Updated title"
    assert task.due == "2025-12-25"
    client.patch.assert_awaited_once()
    assert client.patch.call_args.kwargs["json"] == {
        "title": "Updated title",
        "notes": "new notes",
        "due": "2025-12-25T00:00:00.000Z",
    }


async def test_update_task_noop_reads_current(provider: GoogleTasksProvider) -> None:
    """With no fields to change, update GETs the task instead of PATCHing."""
    client = _tasks_client(_GOOGLE_TASK)
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=client):
        task = await provider.update_task(TENANT, "goog-task-1")

    assert task.id == "goog-task-1"
    client.get.assert_awaited_once()
    client.patch.assert_not_awaited()


async def test_get_task_returns_task(provider: GoogleTasksProvider) -> None:
    """get_task GETs a single task and parses it (backs resolver / attachments)."""
    with patch(
        "epicurus_tasks.google_provider.httpx.AsyncClient",
        return_value=_tasks_client(_GOOGLE_TASK),
    ):
        task = await provider.get_task(TENANT, "goog-task-1")

    assert task is not None
    assert task.id == "goog-task-1"
    assert task.title == "Write tests"


async def test_get_task_missing_returns_none(provider: GoogleTasksProvider) -> None:
    """A 404 from Google resolves to None, not an error, so the proxy returns a clean 404."""
    client = _tasks_client(None)
    client.get = AsyncMock(return_value=_FakeResponse(None, status_code=404))
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=client):
        assert await provider.get_task(TENANT, "missing-id") is None


async def test_due_date_stripped_to_date(provider: GoogleTasksProvider) -> None:
    """RFC 3339 due timestamp should be stripped to ISO date (YYYY-MM-DD)."""
    task_data = {
        "id": "x",
        "title": "T",
        "due": "2025-11-30T00:00:00.000Z",
        "status": "needsAction",
    }
    with patch(
        "epicurus_tasks.google_provider.httpx.AsyncClient",
        return_value=_tasks_client({"items": [task_data]}),
    ):
        tasks = await provider.list_tasks(TENANT)

    assert tasks[0].due == "2025-11-30"
