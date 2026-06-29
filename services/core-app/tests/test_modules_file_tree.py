"""Tests for the file-tree proxy methods on ModuleRegistry (#216).

Each test stubs out httpx.AsyncClient to avoid real HTTP, following the
same pattern used in test_modules_registry.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from epicurus_core import ModuleManifest, PageSpec, SecretError
from epicurus_core_app.modules import ModuleRegistry, ModuleSnapshot, ModuleStatus

# ── Minimal fakes ──────────────────────────────────────────────────────────────


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
        name="knowledge",
        version="0.9.0",
        pages=[PageSpec(id="vault", title="Knowledge", archetype="editor")],
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


def _mock_client(response_data: dict[str, Any] | None = None, *, status_code: int = 200) -> Any:
    """Return a context-manager-compatible AsyncMock for httpx.AsyncClient."""
    mock_response = MagicMock()
    if response_data is not None:
        mock_response.json.return_value = response_data
    mock_response.status_code = status_code

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.delete = AsyncMock(return_value=mock_response)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_client


# ── create_page_folder ─────────────────────────────────────────────────────────


async def test_create_page_folder_calls_module_post() -> None:
    registry = _registry(_editor_manifest())
    ctx, mock_client = _mock_client({"path": "ideas"})

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.create_page_folder("knowledge", "vault", "ideas")

    assert result == {"path": "ideas"}
    mock_client.post.assert_called_once_with("/pages/vault/folder", params={"path": "ideas"})


async def test_create_page_folder_404_for_non_editor() -> None:
    registry = _registry(_browser_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.create_page_folder("files", "browse", "ideas")
    assert err.value.status_code == 404


async def test_create_page_folder_404_for_unknown_module() -> None:
    registry = _registry(_editor_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.create_page_folder("ghost", "vault", "ideas")
    assert err.value.status_code == 404


# ── delete_page_doc ────────────────────────────────────────────────────────────


async def test_delete_page_doc_calls_module_delete() -> None:
    registry = _registry(_editor_manifest())
    ctx, mock_client = _mock_client()

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        await registry.delete_page_doc("knowledge", "vault", "alpha.md")

    mock_client.delete.assert_called_once_with("/pages/vault/doc", params={"path": "alpha.md"})


async def test_delete_page_doc_404_for_non_editor() -> None:
    registry = _registry(_browser_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.delete_page_doc("files", "browse", "alpha.md")
    assert err.value.status_code == 404


# ── delete_page_folder ─────────────────────────────────────────────────────────


async def test_delete_page_folder_calls_module_delete() -> None:
    registry = _registry(_editor_manifest())
    ctx, mock_client = _mock_client()

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        await registry.delete_page_folder("knowledge", "vault", "projects")

    mock_client.delete.assert_called_once_with("/pages/vault/folder", params={"path": "projects"})


async def test_delete_page_folder_404_for_non_editor() -> None:
    registry = _registry(_browser_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.delete_page_folder("files", "browse", "projects")
    assert err.value.status_code == 404


# ── move_page_item ─────────────────────────────────────────────────────────────


async def test_move_page_item_calls_module_post_with_body() -> None:
    registry = _registry(_editor_manifest())
    ctx, mock_client = _mock_client({"path": "renamed.md"})

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.move_page_item("knowledge", "vault", "alpha.md", "renamed.md")

    assert result == {"path": "renamed.md"}
    mock_client.post.assert_called_once_with(
        "/pages/vault/move",
        json={"from_path": "alpha.md", "to_path": "renamed.md"},
    )


async def test_move_page_item_works_for_browser_page() -> None:
    # Move is the one mutation a browser page shares with an editor — the Files browser
    # renames/relocates its writable entries through the same contract (#391).
    registry = _registry(_browser_manifest())
    ctx, mock_client = _mock_client({"path": "b.md"})

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.move_page_item("files", "browse", "a.md", "b.md")

    assert result == {"path": "b.md"}
    mock_client.post.assert_called_once_with(
        "/pages/browse/move",
        json={"from_path": "a.md", "to_path": "b.md"},
    )


async def test_move_page_item_404_for_unsupported_archetype() -> None:
    # A board (or any non-editor/-browser) page has no movable items — move 404s.
    manifest = ModuleManifest(
        name="tasks",
        version="0.1.0",
        pages=[PageSpec(id="board", title="Tasks", archetype="board")],
    )
    registry = _registry(manifest)
    with pytest.raises(HTTPException) as err:
        await registry.move_page_item("tasks", "board", "a", "b")
    assert err.value.status_code == 404


async def test_move_page_item_404_for_unknown_module() -> None:
    registry = _registry(_editor_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.move_page_item("ghost", "vault", "a.md", "b.md")
    assert err.value.status_code == 404
