"""Core-emitted spine events (#665): files.* at the API seam, core.suggestion_* at the funnel.

The fake bus replaces only the wire — everything still goes through the real
``emit_event``, so envelope validation (module-prefixed types, payload caps, the
credential-shaped-key screen) is exercised on every emission.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from epicurus_core import ModuleManifest, PageSpec
from epicurus_core.files import LocalFileStore
from epicurus_core_app.core_events import CoreEventEmitter
from epicurus_core_app.files_routes import create_files_router
from epicurus_core_app.modules import ModuleRegistry, ModuleSnapshot, ModuleStatus

TENANT = "local"

WRITE = "/platform/v1/files/write"
MOVE = "/platform/v1/files/move"
UPLOAD = "/platform/v1/files/upload"
ROOT = "/platform/v1/files"
ENTRY = "/platform/v1/files/entry"


class _FakeBus:
    """Records spine publishes — the repo-standard EventBus stand-in."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], str | None]] = []

    async def publish(
        self, subject: str, data: dict[str, Any], tenant_id: str | None = None
    ) -> None:
        self.published.append((subject, data, tenant_id))

    def subjects(self) -> list[str]:
        return [s for s, _, _ in self.published]


# ── files.* at the file-API seam ────────────────────────────────────────────────


@pytest.fixture
async def files_env(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, _FakeBus]]:
    bus = _FakeBus()
    app = FastAPI()
    app.include_router(
        create_files_router(
            LocalFileStore(tmp_path),
            default_tenant=TENANT,
            events=CoreEventEmitter(bus),  # type: ignore[arg-type]
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        yield client, bus


async def test_new_write_emits_file_added(files_env: tuple[AsyncClient, _FakeBus]) -> None:
    client, bus = files_env
    resp = await client.put(WRITE, params={"path": "docs/a.md"}, json={"content": "hello"})
    assert resp.status_code == 200
    [(subject, data, tenant)] = bus.published
    assert subject == "events.files.file_added"
    assert tenant == TENANT
    assert data["payload"]["path"] == "docs/a.md"
    assert data["entity_ref"]["kind"] == "file"


async def test_overwrite_emits_nothing(files_env: tuple[AsyncClient, _FakeBus]) -> None:
    """No file_updated by design (#665): content owners emit their own *.updated events."""
    client, bus = files_env
    await client.put(WRITE, params={"path": "docs/a.md"}, json={"content": "v1"})
    await client.put(WRITE, params={"path": "docs/a.md"}, json={"content": "v2"})
    assert bus.subjects() == ["events.files.file_added"]  # only the first write announced


async def test_delete_emits_file_deleted(files_env: tuple[AsyncClient, _FakeBus]) -> None:
    client, bus = files_env
    await client.put(WRITE, params={"path": "docs/a.md"}, json={"content": "x"})
    resp = await client.delete(ROOT, params={"path": "docs/a.md"})
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    assert bus.subjects() == ["events.files.file_added", "events.files.file_deleted"]


async def test_delete_of_missing_path_emits_nothing(
    files_env: tuple[AsyncClient, _FakeBus],
) -> None:
    client, bus = files_env
    resp = await client.delete(ROOT, params={"path": "nope.md"})
    assert resp.status_code == 200 and resp.json()["deleted"] is False
    assert bus.published == []


async def test_move_emits_file_moved(files_env: tuple[AsyncClient, _FakeBus]) -> None:
    client, bus = files_env
    await client.put(WRITE, params={"path": "docs/a.md"}, json={"content": "x"})
    resp = await client.post(MOVE, json={"src": "docs/a.md", "dst": "docs/b.md"})
    assert resp.status_code == 200
    assert bus.subjects()[-1] == "events.files.file_moved"
    payload = bus.published[-1][1]["payload"]
    assert payload == {"from_path": "docs/a.md", "to_path": "docs/b.md"}


async def test_upload_emits_file_added(files_env: tuple[AsyncClient, _FakeBus]) -> None:
    client, bus = files_env
    resp = await client.post(
        UPLOAD,
        params={"dir": ""},
        files={"file": ("note.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 200
    [(subject, data, _)] = bus.published
    assert subject == "events.files.file_added"
    assert data["payload"]["path"] == "note.txt"
    assert data["payload"]["size"] == 2


async def test_entry_delete_emits_file_deleted(files_env: tuple[AsyncClient, _FakeBus]) -> None:
    client, bus = files_env
    await client.put(WRITE, params={"path": "docs/a.md"}, json={"content": "x"})
    resp = await client.delete(ENTRY, params={"path": "docs"})
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    # One event per API action: the folder delete is one deletion, not one per file.
    assert bus.subjects() == ["events.files.file_added", "events.files.file_deleted"]
    assert bus.published[-1][1]["payload"]["path"] == "docs"


# ── core.suggestion_* at the review funnel ──────────────────────────────────────


class _FakeMcp:
    async def call(self, *args: Any, **kwargs: Any) -> str:
        return ""


class _FakeSecrets:
    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        return {}

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        pass


class _FakeModulePrefs:
    async def enabled_map(self, tenant: str) -> dict[str, bool]:
        return {}

    async def is_enabled(self, tenant: str, module: str) -> bool:
        return True

    async def removed_modules(self, tenant: str) -> set[str]:
        return set()


def _review_manifest(name: str = "knowledge") -> ModuleManifest:
    return ModuleManifest(
        name=name,
        version="0.1.0",
        pages=[PageSpec(id="review", title="Suggestions", archetype="review")],
    )


class _StubRegistry(ModuleRegistry):
    def __init__(self, *, manifest: ModuleManifest, **kwargs: Any) -> None:
        super().__init__(["http://module:8080"], **kwargs)
        self._manifest = manifest

    async def _probe(self, base: str) -> ModuleSnapshot:
        return ModuleSnapshot(manifest=self._manifest, status=ModuleStatus(healthy=True))


def _registry(bus: _FakeBus, *, core: Any = None) -> _StubRegistry:
    return _StubRegistry(
        manifest=_review_manifest(),
        mcp=_FakeMcp(),  # type: ignore[arg-type]
        secrets=_FakeSecrets(),  # type: ignore[arg-type]
        tenant=TENANT,
        prefs=_FakeModulePrefs(),  # type: ignore[arg-type]
        core=core,
        events=CoreEventEmitter(bus),  # type: ignore[arg-type]
    )


def _mock_client(response_data: dict[str, Any]) -> Any:
    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


APPLY_APPROVED = {"id": "s1", "status": "approved", "path": "kb/a.md", "operation": "update"}
APPLY_REJECTED = {"id": "s1", "status": "rejected", "path": "kb/a.md", "operation": "update"}


async def test_proxied_approve_emits_exactly_one_decision_event() -> None:
    """The #665 acceptance: approve on a module review surface → one decision event."""
    bus = _FakeBus()
    registry = _registry(bus)
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as client_cls:
        client_cls.return_value = _mock_client(APPLY_APPROVED)
        result = await registry.review_action("knowledge", "review", "s1", "approve")

    assert result["status"] == "approved"
    [(subject, data, tenant)] = bus.published
    assert subject == "events.core.suggestion_approved"
    assert tenant == TENANT
    assert data["payload"] == {
        "module": "knowledge",
        "page": "review",
        "sid": "s1",
        "operation": "update",
        "path": "kb/a.md",
    }
    assert data["entity_ref"]["module"] == "knowledge"
    assert data["entity_ref"]["kind"] == "suggestion"
    assert data["dedup_key"] == "knowledge:s1:approved"


async def test_proxied_reject_emits_rejected_event() -> None:
    bus = _FakeBus()
    registry = _registry(bus)
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as client_cls:
        client_cls.return_value = _mock_client(APPLY_REJECTED)
        await registry.review_action("knowledge", "review", "s1", "reject")
    assert bus.subjects() == ["events.core.suggestion_rejected"]


async def test_core_pseudo_module_decision_uses_the_same_seam() -> None:
    """The #665 acceptance: the core-hosted surface (ADR-0093) rides the identical funnel."""

    class _CoreStub:
        def manifest(self) -> ModuleManifest:
            return ModuleManifest(
                name="core",
                version="0.0.0",
                pages=[PageSpec(id="playbooks", title="Playbooks", archetype="review")],
            )

        async def review_action(
            self, page_id: str, suggestion_id: str, action: str, content: str | None = None
        ) -> dict[str, Any]:
            return {
                "id": suggestion_id,
                "status": "approved",
                "path": "morning-brief",
                "operation": "update",
            }

        async def review_audit(self, page_id: str, *, limit: int = 50) -> dict[str, Any]:
            return {"decisions": []}

    bus = _FakeBus()
    registry = _registry(bus, core=_CoreStub())
    await registry.review_action("core", "playbooks", "p1", "approve")

    [(subject, data, _)] = bus.published
    assert subject == "events.core.suggestion_approved"
    assert data["payload"]["module"] == "core"
    assert data["payload"]["page"] == "playbooks"


async def test_unrecognized_status_emits_nothing() -> None:
    bus = _FakeBus()
    registry = _registry(bus)
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as client_cls:
        client_cls.return_value = _mock_client({"weird": True})
        await registry.review_action("knowledge", "review", "s1", "approve")
    assert bus.published == []
