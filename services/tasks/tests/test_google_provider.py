"""Tests for GoogleTasksProvider with mocked httpx client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from epicurus_tasks.google_provider import GoogleTasksError, GoogleTasksProvider

TENANT = "test-tenant"
PLATFORM_URL = "http://core-app:8080"
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


def _mock_platform_resp(token: str = MOCK_TOKEN) -> dict[str, Any]:
    return {"access_token": token, "token_type": "Bearer", "expires_at": None}


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


def _make_clients(token_resp: Any, tasks_resp: Any) -> tuple[AsyncMock, AsyncMock]:
    """Return a pair of context-manager mocks: (platform client, tasks client)."""
    token_client = AsyncMock()
    token_client.__aenter__ = AsyncMock(return_value=token_client)
    token_client.__aexit__ = AsyncMock(return_value=False)
    token_client.get = AsyncMock(return_value=_FakeResponse(_mock_platform_resp(token_resp)))

    tasks_client = AsyncMock()
    tasks_client.__aenter__ = AsyncMock(return_value=tasks_client)
    tasks_client.__aexit__ = AsyncMock(return_value=False)
    tasks_client.get = AsyncMock(return_value=_FakeResponse(tasks_resp))
    tasks_client.post = AsyncMock(return_value=_FakeResponse(tasks_resp))
    tasks_client.patch = AsyncMock(return_value=_FakeResponse(tasks_resp))

    return token_client, tasks_client


@pytest.fixture()
def provider() -> GoogleTasksProvider:
    return GoogleTasksProvider(platform_url=PLATFORM_URL)


async def test_provider_name(provider: GoogleTasksProvider) -> None:
    assert provider.provider_name() == "google"


async def test_list_tasks(provider: GoogleTasksProvider) -> None:
    token_client, tasks_client = _make_clients(MOCK_TOKEN, {"items": [_GOOGLE_TASK]})
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient") as mock_cls:
        mock_cls.side_effect = [token_client, tasks_client]
        tasks = await provider.list_tasks(TENANT)

    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "goog-task-1"
    assert t.title == "Write tests"
    assert t.notes == "Cover all edge cases"
    assert t.due == "2025-06-15"
    assert not t.completed


async def test_list_tasks_empty(provider: GoogleTasksProvider) -> None:
    token_client, tasks_client = _make_clients(MOCK_TOKEN, {"items": []})
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient") as mock_cls:
        mock_cls.side_effect = [token_client, tasks_client]
        tasks = await provider.list_tasks(TENANT)

    assert tasks == []


async def test_list_tasks_missing_items_key(provider: GoogleTasksProvider) -> None:
    """Google omits 'items' when the list is empty."""
    token_client, tasks_client = _make_clients(MOCK_TOKEN, {})
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient") as mock_cls:
        mock_cls.side_effect = [token_client, tasks_client]
        tasks = await provider.list_tasks(TENANT)

    assert tasks == []


async def test_add_task(provider: GoogleTasksProvider) -> None:
    new_task = {
        "id": "new-task-id",
        "title": "Ship the feature",
        "status": "needsAction",
    }
    token_client, tasks_client = _make_clients(MOCK_TOKEN, new_task)
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient") as mock_cls:
        mock_cls.side_effect = [token_client, tasks_client]
        task = await provider.add_task(TENANT, "Ship the feature", due="2025-07-01")

    assert task.id == "new-task-id"
    assert task.title == "Ship the feature"


async def test_complete_task(provider: GoogleTasksProvider) -> None:
    token_client, tasks_client = _make_clients(MOCK_TOKEN, _GOOGLE_TASK_COMPLETED)
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient") as mock_cls:
        mock_cls.side_effect = [token_client, tasks_client]
        task = await provider.complete_task(TENANT, "goog-task-1")

    assert task.completed
    assert task.completed_at == "2025-06-14T10:00:00.000Z"


async def test_not_connected_raises(provider: GoogleTasksProvider) -> None:
    """A 404 from the token endpoint means the provider is not connected."""
    token_client = AsyncMock()
    token_client.__aenter__ = AsyncMock(return_value=token_client)
    token_client.__aexit__ = AsyncMock(return_value=False)
    token_client.get = AsyncMock(return_value=_FakeResponse({}, status_code=404))

    with (
        patch("epicurus_tasks.google_provider.httpx.AsyncClient", return_value=token_client),
        pytest.raises(GoogleTasksError, match="not connected"),
    ):
        await provider.list_tasks(TENANT)


async def test_due_date_stripped_to_date(provider: GoogleTasksProvider) -> None:
    """RFC 3339 due timestamp should be stripped to ISO date (YYYY-MM-DD)."""
    task_data = {
        "id": "x",
        "title": "T",
        "due": "2025-11-30T00:00:00.000Z",
        "status": "needsAction",
    }
    token_client, tasks_client = _make_clients(MOCK_TOKEN, {"items": [task_data]})
    with patch("epicurus_tasks.google_provider.httpx.AsyncClient") as mock_cls:
        mock_cls.side_effect = [token_client, tasks_client]
        tasks = await provider.list_tasks(TENANT)

    assert tasks[0].due == "2025-11-30"
