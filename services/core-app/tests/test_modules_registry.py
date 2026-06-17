"""Unit tests for the module registry — module probing is stubbed in-process."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from epicurus_core import (
    ModelSlot,
    ModuleManifest,
    PageSpec,
    SecretError,
    ToolSpec,
    UiAction,
    UiSection,
)
from epicurus_core_app.docker_control import DockerError
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


class _FakeModulePrefs:
    """In-memory stand-in for ModulePrefsStore (no DB) — a module defaults to enabled."""

    def __init__(self) -> None:
        self.flags: dict[tuple[str, str], bool] = {}
        self.removed: set[tuple[str, str]] = set()
        self.models: dict[tuple[str, str], dict[str, str]] = {}

    async def enabled_map(self, tenant: str) -> dict[str, bool]:
        return {m: e for (t, m), e in self.flags.items() if t == tenant}

    async def is_enabled(self, tenant: str, module: str) -> bool:
        return self.flags.get((tenant, module), True)

    async def set_enabled(self, tenant: str, module: str, enabled: bool) -> None:
        self.flags[(tenant, module)] = enabled

    async def removed_modules(self, tenant: str) -> set[str]:
        return {m for (t, m) in self.removed if t == tenant}

    async def set_removed(self, tenant: str, module: str, removed: bool) -> None:
        if removed:
            self.removed.add((tenant, module))
        else:
            self.removed.discard((tenant, module))

    async def get_models(self, tenant: str, module: str) -> dict[str, str]:
        return dict(self.models.get((tenant, module), {}))

    async def set_models(self, tenant: str, module: str, models: dict[str, str]) -> None:
        self.models[(tenant, module)] = dict(models)


class _FakeDocker:
    """In-memory stand-in for DockerController — records removals; never touches Docker."""

    def __init__(self, *, count: int = 1, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._count = count
        self._error = error

    def remove_module(self, name: str) -> int:
        self.calls.append(name)
        if self._error is not None:
            raise self._error
        return self._count


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
    *,
    healthy: bool = True,
    manifest: ModuleManifest | None = None,
    docker: _FakeDocker | None = None,
) -> tuple[_StubRegistry, _FakeMcp, _FakeSecrets]:
    mcp, secrets = _FakeMcp(), _FakeSecrets()
    registry = _StubRegistry(  # type: ignore[arg-type]
        healthy=healthy,
        manifest=manifest,
        mcp=mcp,
        secrets=secrets,
        tenant="local",
        prefs=_FakeModulePrefs(),
        docker=docker,
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


# ── Enable / disable (#126) ───────────────────────────────────────────────────


async def test_snapshot_defaults_to_enabled() -> None:
    registry, _, _ = _registry()
    snaps = await registry.snapshot()
    assert snaps[0].enabled is True


async def test_snapshot_reflects_disabled_flag() -> None:
    registry, _, _ = _registry()
    await registry.set_enabled("echo", False)
    snaps = await registry.snapshot()
    assert snaps[0].enabled is False


async def test_enabled_mcp_urls_includes_enabled_module() -> None:
    registry, _, _ = _registry()
    assert await registry.enabled_mcp_urls() == ["http://echo:8080/mcp"]


async def test_enabled_mcp_urls_excludes_disabled_module() -> None:
    registry, _, _ = _registry()
    await registry.set_enabled("echo", False)
    assert await registry.enabled_mcp_urls() == []


async def test_enabled_mcp_urls_excludes_unhealthy_module() -> None:
    registry, _, _ = _registry(healthy=False)
    assert await registry.enabled_mcp_urls() == []


async def test_invoke_disabled_module_is_403() -> None:
    registry, _, _ = _registry()
    await registry.set_enabled("echo", False)
    with pytest.raises(HTTPException) as err:
        await registry.invoke("echo", "echo", {"message": "hi"})
    assert err.value.status_code == 403


async def test_re_enable_restores_invoke() -> None:
    registry, _, _ = _registry()
    await registry.set_enabled("echo", False)
    await registry.set_enabled("echo", True)
    assert await registry.invoke("echo", "echo", {}) == "ran"


async def test_set_enabled_unknown_module_is_404() -> None:
    registry, _, _ = _registry()
    with pytest.raises(HTTPException) as err:
        await registry.set_enabled("ghost", False)
    assert err.value.status_code == 404


# ── Removal (#127) ─────────────────────────────────────────────────────────────


async def test_remove_tombstones_and_hides_module() -> None:
    docker = _FakeDocker(count=1)
    registry, _, _ = _registry(docker=docker)
    result = await registry.remove("echo")
    assert result == {"removed": "echo", "containers": 1}
    assert docker.calls == ["echo"]
    # Tombstoned: still 1:1 with bases (flagged), excluded from discovery, dropped by list.
    snaps = await registry.snapshot()
    assert snaps[0].removed is True
    assert await registry.enabled_mcp_urls() == []


async def test_remove_unknown_module_is_404() -> None:
    registry, _, _ = _registry(docker=_FakeDocker())
    with pytest.raises(HTTPException) as err:
        await registry.remove("ghost")
    assert err.value.status_code == 404


async def test_remove_without_docker_is_503() -> None:
    registry, _, _ = _registry(docker=None)
    with pytest.raises(HTTPException) as err:
        await registry.remove("echo")
    assert err.value.status_code == 503


async def test_remove_protected_propagates_as_403() -> None:
    docker = _FakeDocker(error=DockerError("'echo' is protected and cannot be removed"))
    registry, _, _ = _registry(docker=docker)
    with pytest.raises(HTTPException) as err:
        await registry.remove("echo")
    assert err.value.status_code == 403


async def test_reconcile_tombstones_re_removes_resurrected() -> None:
    docker = _FakeDocker(count=1)
    registry, _, _ = _registry(docker=docker)
    await registry.remove("echo")
    docker.calls.clear()
    await registry.reconcile_tombstones()
    assert docker.calls == ["echo"]


async def test_reconcile_without_docker_is_noop() -> None:
    registry, _, _ = _registry(docker=None)
    await registry.reconcile_tombstones()  # must not raise


# ── Path-segment hardening (#175): reject '/', '\', '..' in interpolated segments ──


async def test_resolve_entity_rejects_unsafe_segment() -> None:
    registry, _, _ = _registry(manifest=_resolver_manifest())
    for kind, ref_id in (("event", "../secret"), ("a/b", "e1"), ("event", "a/b")):
        with pytest.raises(HTTPException) as err:
            await registry.resolve_entity("calendar", kind, ref_id)
        assert err.value.status_code == 400


async def test_resolve_attachment_rejects_unsafe_ref() -> None:
    registry, _, _ = _registry(manifest=_attachable_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.resolve_attachment("notes", "../n1")
    assert err.value.status_code == 400


async def test_read_message_rejects_unsafe_ref() -> None:
    registry, _, _ = _registry()
    with pytest.raises(HTTPException) as err:
        await registry.read_message("echo", "a/b")
    assert err.value.status_code == 400


async def test_get_page_rejects_unsafe_page_id() -> None:
    registry, _, _ = _registry(manifest=_pages_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_page("files", "..")
    assert err.value.status_code == 400


# ── Per-module model selection (#128) ──────────────────────────────────────────


def _models_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="knowledge",
        version="0.6.0",
        required_models=[ModelSlot(key="embedding", role="embedding", label="Embedding model")],
    )


async def test_set_and_get_models() -> None:
    registry, _, _ = _registry(manifest=_models_manifest())
    await registry.set_models("knowledge", {"embedding": "nomic-embed-text"})
    assert await registry.get_models("knowledge") == {"embedding": "nomic-embed-text"}


async def test_set_models_rejects_unknown_slot() -> None:
    registry, _, _ = _registry(manifest=_models_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.set_models("knowledge", {"bogus": "x"})
    assert err.value.status_code == 400


async def test_set_models_drops_blank_selection() -> None:
    registry, _, _ = _registry(manifest=_models_manifest())
    await registry.set_models("knowledge", {"embedding": ""})  # blank → fall back to default
    assert await registry.get_models("knowledge") == {}


async def test_model_for_slot_returns_choice_or_none() -> None:
    registry, _, _ = _registry(manifest=_models_manifest())
    assert await registry.model_for_slot("knowledge", "embedding") is None
    await registry.set_models("knowledge", {"embedding": "nomic-embed-text"})
    assert await registry.model_for_slot("knowledge", "embedding") == "nomic-embed-text"


async def test_get_models_unknown_module_is_404() -> None:
    registry, _, _ = _registry(manifest=_models_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_models("ghost")
    assert err.value.status_code == 404
