"""Unit tests for the module registry — module probing is stubbed in-process."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from epicurus_core import ModuleManifest, PageSpec, SecretError, ToolSpec, UiAction, UiSection
from epicurus_core_app.modules import ModuleRegistry, ModuleSnapshot, ModuleStatus


class _FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def call(self, name: str, arguments: dict[str, Any], url: str) -> str:
        self.calls.append((name, arguments, url))
        return "ran"


class _FakeSecrets:
    def __init__(self) -> None:
        self.stored: dict[str, dict[str, Any]] = {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if path not in self.stored:
            raise SecretError("missing")
        return self.stored[path]

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        self.stored[path] = data


def _echo_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="echo",
        version="0.1.0",
        tools=[ToolSpec(name="echo", input_schema={"type": "object"})],
        ui=UiSection(summary="echoes", actions=[UiAction(tool="echo", label="Send")]),
    )


def _knowledge_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="knowledge",
        version="0.2.0",
        ui=UiSection(summary="vault RAG", status_url="/status", actions=[]),
    )


def _pages_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="files",
        version="0.1.0",
        pages=[PageSpec(id="browse", title="Files", archetype="browser")],
    )


def _editor_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="knowledge",
        version="0.4.0",
        pages=[PageSpec(id="vault", title="Knowledge", archetype="editor")],
    )


def _resolver_manifest() -> ModuleManifest:
    return ModuleManifest(name="calendar", version="0.1.0", resolver=True)


def _attachable_manifest() -> ModuleManifest:
    return ModuleManifest(name="notes", version="0.1.0", attachable=True)


class _StubRegistry(ModuleRegistry):
    """Registry with the network probe replaced by a canned snapshot."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        manifest: ModuleManifest | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(["http://echo:8080"], **kwargs)
        self._healthy = healthy
        self._manifest = manifest or _echo_manifest()

    async def _probe(self, base: str) -> ModuleSnapshot:
        return ModuleSnapshot(manifest=self._manifest, status=ModuleStatus(healthy=self._healthy))


def _registry(
    *, healthy: bool = True, manifest: ModuleManifest | None = None
) -> tuple[_StubRegistry, _FakeMcp, _FakeSecrets]:
    mcp, secrets = _FakeMcp(), _FakeSecrets()
    registry = _StubRegistry(  # type: ignore[arg-type]
        healthy=healthy, manifest=manifest, mcp=mcp, secrets=secrets, tenant="local"
    )
    return registry, mcp, secrets


async def test_invoke_routes_to_the_module_mcp_endpoint() -> None:
    registry, mcp, _ = _registry()
    result = await registry.invoke("echo", "echo", {"message": "hi"})
    assert result == "ran"
    assert mcp.calls == [("echo", {"message": "hi"}, "http://echo:8080/mcp")]


async def test_invoke_unknown_module_is_404() -> None:
    registry, _, _ = _registry()
    with pytest.raises(HTTPException) as err:
        await registry.invoke("ghost", "echo", {})
    assert err.value.status_code == 404


async def test_invoke_unknown_tool_is_404() -> None:
    registry, _, _ = _registry()
    with pytest.raises(HTTPException) as err:
        await registry.invoke("echo", "rm_rf", {})
    assert err.value.status_code == 404


async def test_invoke_unreachable_module_is_404() -> None:
    registry, _, _ = _registry(healthy=False)
    with pytest.raises(HTTPException) as err:
        await registry.invoke("echo", "echo", {})
    assert err.value.status_code == 404


async def test_config_round_trip_and_empty_default() -> None:
    registry, _, secrets = _registry()
    assert await registry.get_config("echo") == {}
    await registry.set_config("echo", {"greeting": "hi"})
    assert await registry.get_config("echo") == {"greeting": "hi"}
    assert "modules/echo/config" in secrets.stored


async def test_set_config_for_unknown_module_is_404() -> None:
    registry, _, _ = _registry()
    with pytest.raises(HTTPException):
        await registry.set_config("ghost", {"a": 1})


async def test_get_status_proxies_module_status_url() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_knowledge_manifest())

    # httpx Response.raise_for_status() and .json() are synchronous.
    mock_response = MagicMock()
    mock_response.json.return_value = {"note_count": 42, "last_indexed_at": "2026-06-13T10:00:00"}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.get_status("knowledge")

    assert result["note_count"] == 42
    mock_client.get.assert_called_once_with("/status")


async def test_get_status_404_when_no_status_url() -> None:
    registry, _, _ = _registry()  # echo manifest has no status_url
    with pytest.raises(HTTPException) as err:
        await registry.get_status("echo")
    assert err.value.status_code == 404


async def test_get_status_404_for_unknown_module() -> None:
    registry, _, _ = _registry(manifest=_knowledge_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_status("ghost")
    assert err.value.status_code == 404


async def test_get_page_proxies_declared_page() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_pages_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = {"title": "Files", "items": [{"id": "a", "title": "a"}]}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.get_page("files", "browse")

    assert result["items"][0]["id"] == "a"
    mock_client.get.assert_called_once_with("/pages/browse", params={})


async def test_get_page_forwards_params() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_pages_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = {"title": "Files", "items": []}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await registry.get_page("files", "browse", params={"path": "docs", "q": ""})

    mock_client.get.assert_called_once_with("/pages/browse", params={"path": "docs", "q": ""})


async def test_get_page_404_for_undeclared_page() -> None:
    registry, _, _ = _registry(manifest=_pages_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_page("files", "ghost")
    assert err.value.status_code == 404


async def test_get_page_404_for_unknown_module() -> None:
    registry, _, _ = _registry(manifest=_pages_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_page("ghost", "browse")
    assert err.value.status_code == 404


async def test_get_page_forwards_query_params() -> None:
    """A parameterized archetype (e.g. a calendar's start/end) reaches the module."""
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_pages_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = {"events": []}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.get_page("files", "browse", params={"start": "s", "end": "e"})

    assert result == {"events": []}
    mock_client.get.assert_called_once_with("/pages/browse", params={"start": "s", "end": "e"})


async def test_get_page_doc_proxies_editor_document() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_editor_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = {"path": "a.md", "title": "a", "content": "# A"}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.get_page_doc("knowledge", "vault", "a.md")

    assert result["content"] == "# A"
    mock_client.get.assert_called_once_with("/pages/vault/doc", params={"path": "a.md"})


async def test_save_page_doc_proxies_put_with_body() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_editor_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = {"path": "a.md", "indexed": True, "chunk_count": 2}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.put = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.save_page_doc("knowledge", "vault", "a.md", "# A")

    assert result["indexed"] is True
    mock_client.put.assert_called_once_with(
        "/pages/vault/doc", params={"path": "a.md"}, json={"content": "# A"}
    )


async def test_get_page_doc_404_for_non_editor_page() -> None:
    # A browser page owns no per-document read/write — the doc paths 404 for it.
    registry, _, _ = _registry(manifest=_pages_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_page_doc("files", "browse", "a.md")
    assert err.value.status_code == 404


async def test_get_page_doc_404_for_unknown_page() -> None:
    registry, _, _ = _registry(manifest=_editor_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_page_doc("knowledge", "ghost", "a.md")
    assert err.value.status_code == 404


async def test_save_page_doc_404_for_non_editor_page() -> None:
    registry, _, _ = _registry(manifest=_pages_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.save_page_doc("files", "browse", "a.md", "x")
    assert err.value.status_code == 404


async def test_resolve_entity_proxies_module_resolver() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_resolver_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = {"title": "Standup", "description": "9am", "details": []}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.resolve_entity("calendar", "event", "e1")

    assert result["title"] == "Standup"
    mock_client.get.assert_called_once_with("/resolve/event/e1")


async def test_resolve_entity_404_when_no_resolver() -> None:
    registry, _, _ = _registry()  # echo manifest declares no resolver
    with pytest.raises(HTTPException) as err:
        await registry.resolve_entity("echo", "event", "e1")
    assert err.value.status_code == 404


async def test_resolve_entity_404_for_unknown_module() -> None:
    registry, _, _ = _registry(manifest=_resolver_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.resolve_entity("ghost", "event", "e1")
    assert err.value.status_code == 404


async def test_list_attachments_proxies_picker() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_attachable_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = [{"ref_id": "n1", "kind": "note", "title": "Groceries"}]

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.list_attachments("notes")

    assert result[0]["ref_id"] == "n1"
    mock_client.get.assert_called_once_with("/attachments")


async def test_list_attachments_404_when_not_attachable() -> None:
    registry, _, _ = _registry()  # echo is not an attachment source
    with pytest.raises(HTTPException) as err:
        await registry.list_attachments("echo")
    assert err.value.status_code == 404


async def test_resolve_attachment_proxies_module() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_attachable_manifest())

    mock_response = MagicMock()
    mock_response.json.return_value = {"title": "Groceries", "excerpt": "milk, eggs"}

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.resolve_attachment("notes", "n1")

    assert result["excerpt"] == "milk, eggs"
    mock_client.get.assert_called_once_with("/attachments/n1")


async def test_resolve_attachment_404_when_not_attachable() -> None:
    registry, _, _ = _registry()
    with pytest.raises(HTTPException) as err:
        await registry.resolve_attachment("echo", "n1")
    assert err.value.status_code == 404
