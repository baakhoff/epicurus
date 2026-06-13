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
