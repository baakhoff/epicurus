"""HTTP tests for the entity-ref resolver and chat-attachment source (ADR-0019).

These exercise the routes the core proxies — ``GET /attachments``,
``GET /attachments/{ref_id}`` and ``GET /resolve/{kind}/{ref_id}`` — through a
``TestClient`` with a fake in-memory provider, so the wiring (and its 404s) is
covered without a database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from epicurus_core import CollectionPrefs
from epicurus_tasks.models import Task


class _FakeProvider:
    """In-memory tasks provider holding a fixed set of tasks for endpoint tests."""

    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = {t.id: t for t in tasks}

    def provider_name(self) -> str:
        return "local"

    async def list_tasks(
        self, tenant_id: str, *, list_id: str | None = None, scope: str = "open"
    ) -> list[Task]:
        return list(self._tasks.values())

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        return self._tasks.get(task_id)

    async def add_task(self, *a: object, **k: object) -> Task:  # pragma: no cover - unused here
        raise NotImplementedError

    async def complete_task(self, *a: object, **k: object) -> Task:  # pragma: no cover - unused
        raise NotImplementedError

    async def update_task(self, *a: object, **k: object) -> Task:  # pragma: no cover - unused
        raise NotImplementedError


def _sample_task() -> Task:
    return Task(id="t1", title="Write report", notes="Q2 numbers", due="2026-06-20")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("NATS_URL", "nats://localhost:4222")
    fake = _FakeProvider([_sample_task()])
    with (
        patch("epicurus_tasks.app.LocalTasksProvider", return_value=fake),
        patch("epicurus_tasks.app.EventBus.from_settings") as mock_bus_factory,
        # No active selection → the router routes to the local fake, with no core round-trip.
        patch(
            "epicurus_tasks.router.TasksRouter._load_prefs",
            new=AsyncMock(return_value=CollectionPrefs()),
        ),
    ):
        mock_bus_factory.return_value = AsyncMock()
        from epicurus_tasks.app import create_app

        app = create_app()
    return TestClient(app, raise_server_exceptions=True)


class TestResolveEntity:
    def test_task_returns_hovercard(self, client: TestClient) -> None:
        resp = client.get("/resolve/task/t1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Write report"
        assert body["description"] == "Q2 numbers"
        details = {d["label"]: d["value"] for d in body["details"]}
        assert details["Due"] == "2026-06-20"
        assert details["Status"] == "Open"

    def test_unknown_kind_is_404(self, client: TestClient) -> None:
        resp = client.get("/resolve/event/t1")
        assert resp.status_code == 404

    def test_missing_task_is_404(self, client: TestClient) -> None:
        resp = client.get("/resolve/task/nope")
        assert resp.status_code == 404


class TestAttachments:
    def test_picker_lists_tasks(self, client: TestClient) -> None:
        resp = client.get("/attachments")
        assert resp.status_code == 200
        assert resp.json() == [{"ref_id": "t1", "kind": "task", "title": "Write report"}]

    def test_resolve_returns_title_and_excerpt(self, client: TestClient) -> None:
        resp = client.get("/attachments/t1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Write report"
        assert "Q2 numbers" in body["excerpt"]
        assert "2026-06-20" in body["excerpt"]

    def test_resolve_missing_is_404(self, client: TestClient) -> None:
        resp = client.get("/attachments/nope")
        assert resp.status_code == 404
