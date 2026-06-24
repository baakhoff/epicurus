"""Tests for the editor version-history proxy methods on ModuleRegistry (ADR-0046, #290).

Like the file-tree proxy tests, these stub ``httpx.AsyncClient`` so no real HTTP
happens — here we mock ``.get`` (the version reads go through ``_get_json``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from epicurus_core import ModuleManifest, PageSpec, SecretError
from epicurus_core_app.modules import ModuleRegistry, ModuleSnapshot, ModuleStatus

# ── Minimal fakes (mirrors test_modules_file_tree.py) ───────────────────────────


class _FakeMcp:
    async def call(self, name: str, arguments: dict[str, Any], url: str) -> str:
        return "ok"


class _FakeSecrets:
    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        raise SecretError("missing")

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        pass


class _FakeModulePrefs:
    async def enabled_map(self, tenant: str) -> dict[str, bool]:
        return {}

    async def is_enabled(self, tenant: str, module: str) -> bool:
        return True

    async def set_enabled(self, tenant: str, module: str, enabled: bool) -> None:
        pass

    async def removed_modules(self, tenant: str) -> set[str]:
        return set()

    async def set_removed(self, tenant: str, module: str, removed: bool) -> None:
        pass

    async def get_models(self, tenant: str, module: str) -> dict[str, str]:
        return {}

    async def set_models(self, tenant: str, module: str, models: dict[str, str]) -> None:
        pass

    async def get_disabled_tools(self, tenant: str, module: str) -> set[str]:
        return set()

    async def set_tool_enabled(self, tenant: str, module: str, tool: str, enabled: bool) -> None:
        pass


def _editor_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="notes",
        version="0.3.0",
        pages=[PageSpec(id="notes", title="Notes", archetype="editor")],
    )


def _browser_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="files",
        version="0.1.0",
        pages=[PageSpec(id="browse", title="Files", archetype="browser")],
    )


class _StubRegistry(ModuleRegistry):
    def __init__(self, *, manifest: ModuleManifest, **kwargs: Any) -> None:
        super().__init__(["http://module:8080"], **kwargs)
        self._manifest = manifest

    async def _probe(self, base: str) -> ModuleSnapshot:
        return ModuleSnapshot(manifest=self._manifest, status=ModuleStatus(healthy=True))


def _registry(manifest: ModuleManifest) -> _StubRegistry:
    return _StubRegistry(
        manifest=manifest,
        mcp=_FakeMcp(),  # type: ignore[arg-type]
        secrets=_FakeSecrets(),  # type: ignore[arg-type]
        tenant="local",
        prefs=_FakeModulePrefs(),  # type: ignore[arg-type]
    )


def _mock_get_client(response_data: dict[str, Any]) -> Any:
    """A context-manager-compatible AsyncMock for httpx.AsyncClient with a stubbed GET."""
    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_client


# ── get_page_doc_versions ───────────────────────────────────────────────────────


async def test_get_page_doc_versions_proxies_the_list() -> None:
    registry = _registry(_editor_manifest())
    payload = {
        "versions": [
            {"version_id": "3", "created_at": "2026-06-23T10:00:00+00:00", "title": "N", "size": 12}
        ]
    }
    ctx, mock_client = _mock_get_client(payload)

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.get_page_doc_versions("notes", "notes", "my-note")

    assert result == payload
    mock_client.get.assert_called_once_with("/pages/notes/doc/versions", params={"path": "my-note"})


async def test_get_page_doc_versions_404_for_non_editor() -> None:
    registry = _registry(_browser_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_page_doc_versions("files", "browse", "x")
    assert err.value.status_code == 404


# ── get_page_doc_version ────────────────────────────────────────────────────────


async def test_get_page_doc_version_proxies_one_version() -> None:
    registry = _registry(_editor_manifest())
    payload = {
        "path": "my-note",
        "version_id": "3",
        "created_at": "2026-06-23T10:00:00+00:00",
        "title": "N",
        "content": "# Note",
    }
    ctx, mock_client = _mock_get_client(payload)

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.get_page_doc_version("notes", "notes", "my-note", "3")

    assert result == payload
    mock_client.get.assert_called_once_with(
        "/pages/notes/doc/version", params={"path": "my-note", "version": "3"}
    )


async def test_get_page_doc_version_404_for_unknown_module() -> None:
    registry = _registry(_editor_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_page_doc_version("ghost", "notes", "x", "1")
    assert err.value.status_code == 404
