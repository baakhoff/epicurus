"""Tests for the storage object backend (ADR-0063).

Two contracts are pinned here: the **endpoint URLs** the backend calls on the storage module
(a mismatch is invisible to the smoke gate, which never exercises object read/download/move),
and the **degrade behaviour** the core Files view relies on — when storage is unreachable the
page still renders (object listing is empty) and an object read/download is a clean miss, while
a move surfaces an error. The streaming happy path is proven end-to-end against the real module.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from fastapi import HTTPException

from epicurus_core_app.modules import ModuleRegistry
from epicurus_core_app.object_backend import StorageObjectBackend


class _DownRegistry:
    """A registry whose storage module is never reachable (health-gated 404)."""

    async def base_url(self, name: str) -> str:
        raise HTTPException(status_code=404, detail=f"no reachable module named {name!r}")


class _UpRegistry:
    """A registry that resolves storage to a base URL."""

    async def base_url(self, name: str) -> str:
        return "http://storage:8080"


def _down() -> StorageObjectBackend:
    return StorageObjectBackend(cast("ModuleRegistry", _DownRegistry()))


def _up() -> StorageObjectBackend:
    return StorageObjectBackend(cast("ModuleRegistry", _UpRegistry()))


# ── Degrade behaviour (storage unreachable) ──────────────────────────────────


async def test_list_degrades_to_empty_when_storage_down() -> None:
    assert await _down().list(tenant="local", path="", query="") == []


async def test_read_is_a_clean_miss_when_storage_down() -> None:
    assert await _down().read(tenant="local", path="uploads/x.txt") is None


async def test_download_is_a_clean_miss_when_storage_down() -> None:
    assert await _down().download(tenant="local", path="uploads/x.txt") is None


async def test_move_raises_when_storage_down() -> None:
    with pytest.raises(HTTPException):
        await _down().move(tenant="local", src="uploads/a", dst="uploads/b")


# ── Endpoint URLs the backend calls on storage ───────────────────────────────


class _FakeResp:
    def __init__(self, *, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "http://storage:8080"),
                response=httpx.Response(self.status_code),
            )


class _FakeClient:
    def __init__(self, capture: dict[str, Any], resp: _FakeResp) -> None:
        self._capture = capture
        self._resp = resp

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, params: dict[str, str] | None = None) -> _FakeResp:
        self._capture.update(method="GET", url=url, params=params)
        return self._resp

    async def post(
        self,
        url: str,
        params: dict[str, str] | None = None,
        json: dict[str, str] | None = None,
    ) -> _FakeResp:
        self._capture.update(method="POST", url=url, params=params, json=json)
        return self._resp


def _patch(monkeypatch: pytest.MonkeyPatch, resp: _FakeResp, capture: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "epicurus_core_app.object_backend.httpx.AsyncClient",
        lambda *a, **k: _FakeClient(capture, resp),
    )


async def test_list_hits_objects_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict[str, Any] = {}
    _patch(
        monkeypatch,
        _FakeResp(
            body={"entries": [{"path": "uploads/a", "name": "a", "size": 1, "kind": "file"}]}
        ),
        cap,
    )
    entries = await _up().list(tenant="local", path="uploads", query="")
    assert cap["method"] == "GET" and cap["url"] == "/objects"
    assert cap["params"] == {"tenant_id": "local", "path": "uploads", "q": ""}
    assert [e.path for e in entries] == ["uploads/a"]


async def test_read_hits_objects_read_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, _FakeResp(body={"path": "uploads/a", "name": "a", "content": "hi"}), cap)
    text = await _up().read(tenant="local", path="uploads/a")
    assert cap["url"] == "/objects/read"
    assert cap["params"] == {"tenant_id": "local", "path": "uploads/a"}
    assert text is not None and text.content == "hi"


async def test_move_hits_objects_move_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, _FakeResp(body={"path": "uploads/b"}), cap)
    entry = await _up().move(tenant="local", src="uploads/a", dst="uploads/b")
    assert cap["method"] == "POST" and cap["url"] == "/objects/move"
    assert cap["json"] == {"from_path": "uploads/a", "to_path": "uploads/b"}
    assert entry.path == "uploads/b"
