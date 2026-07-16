"""Unit tests for the module registry — module probing is stubbed in-process."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import structlog
from fastapi import HTTPException
from structlog.testing import capture_logs

from epicurus_core import (
    CollectionPrefs,
    CollectionRef,
    CollectionsSpec,
    ModelSlot,
    ModuleManifest,
    PageSpec,
    SecretError,
    ToolSpec,
    UiAction,
    UiSection,
)
from epicurus_core_app.agent.mcp_host import ModuleUnreachableError, ToolCallError
from epicurus_core_app.docker_control import DockerError
from epicurus_core_app.modules import DockerStatus, ModuleRegistry, ModuleSnapshot, ModuleStatus


@pytest.fixture(autouse=True)
def _unconfigured_structlog() -> Iterator[None]:
    """Isolate every test in this file from another test's ``configure_logging()`` call.

    Whichever test in the full suite happens to boot the real app first (e.g.
    ``test_epicurus_core_app.py`` / ``test_core_app_lifespan.py``, both of which call
    ``create_app()``) pins structlog's global ``wrapper_class`` to filter at the
    process's configured level (``info`` by default) for the rest of the pytest
    process — ``structlog.configure()`` has no per-process scoping. That silently
    drops every ``log.debug(...)`` call afterward, including the ones the
    health-transition tests below assert on via ``capture_logs()``, even though each
    test already constructs its own fresh ``ModuleRegistry``. Reset to structlog's
    built-in (unfiltered) defaults before each test and restore whatever was
    configured beforehand afterward, so this file's log-level assertions hold
    regardless of what ran earlier in the same session.
    """
    was_configured = structlog.is_configured()
    prev_config = structlog.get_config() if was_configured else None
    structlog.reset_defaults()
    yield
    if was_configured and prev_config is not None:
        structlog.configure(**prev_config)
    else:
        structlog.reset_defaults()


class _FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
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
        self.disabled: dict[tuple[str, str], set[str]] = {}
        self.collections: dict[tuple[str, str], CollectionPrefs] = {}
        self.suggestions: dict[tuple[str, str], bool] = {}

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

    async def get_collections(self, tenant: str, module: str) -> CollectionPrefs:
        return self.collections.get((tenant, module), CollectionPrefs())

    async def set_collections(self, tenant: str, module: str, prefs: CollectionPrefs) -> None:
        self.collections[(tenant, module)] = prefs

    async def get_suggestions_enabled(self, tenant: str, module: str) -> bool:
        return self.suggestions.get((tenant, module), True)

    async def set_suggestions_enabled(self, tenant: str, module: str, enabled: bool) -> None:
        self.suggestions[(tenant, module)] = enabled

    async def get_disabled_tools(self, tenant: str, module: str) -> set[str]:
        return set(self.disabled.get((tenant, module), set()))

    async def set_tool_enabled(self, tenant: str, module: str, tool: str, enabled: bool) -> None:
        key = (tenant, module)
        s = set(self.disabled.get(key, set()))
        if enabled:
            s.discard(tool)
        else:
            s.add(tool)
        self.disabled[key] = s


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


def _protected_named_manifest() -> ModuleManifest:
    """A (contrived) module whose name collides with a protected service — to exercise the
    registry's defensive PROTECTED denylist in :meth:`ModuleRegistry.remove` (#382)."""
    return ModuleManifest(name="postgres", version="0.1.0")


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
    docker_reason: str | None = None,
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
        docker_unavailable_reason=docker_reason,
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


async def test_invoke_tool_error_is_400_with_the_tools_message() -> None:
    # A tool that runs but reports failure must surface as an HTTP error carrying the
    # tool's own message — previously the error text returned as a 200 "result" and the
    # shell closed the form as if the action had worked (#435).
    registry, mcp, _ = _registry()

    async def failing_call(name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
        raise ToolCallError("Error executing tool echo: event 'e1' not found")

    mcp.call = failing_call  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as err:
        await registry.invoke("echo", "echo", {"message": "hi"})
    assert err.value.status_code == 400
    assert "event 'e1' not found" in err.value.detail


async def test_invoke_unreachable_module_is_502_not_raw_network_error() -> None:
    # A module that is down / does not answer must surface as a controlled 502, not an
    # unhandled transport exception that nginx turns into an opaque "NetworkError" — the
    # failure every board/calendar action shared through this dispatch (#472). Distinct
    # from the 404 above: there the module is known-unhealthy at resolve time; here it
    # resolves healthy but the tool call itself fails mid-flight.
    registry, mcp, _ = _registry()

    async def unreachable_call(
        name: str, arguments: dict[str, Any], url: str, *, tenant: str
    ) -> str:
        raise ModuleUnreachableError(f"module for tool {name!r} is unreachable")

    mcp.call = unreachable_call  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as err:
        await registry.invoke("echo", "echo", {"message": "hi"})
    assert err.value.status_code == 502
    assert "unreachable" in err.value.detail


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


# ── re-embed fan-out (#332) ────────────────────────────────────────────────────


def _reindexable_manifest() -> ModuleManifest:
    return ModuleManifest(name="knowledge", version="0.16.0", reindexable=True)


class _ReembedRegistry(_StubRegistry):
    """A stub registry that records re-embed fan-out POSTs instead of hitting the network."""

    def __init__(self, *, fail: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.posted: list[str] = []
        self._fail = fail

    async def _post_reindex(self, base: str) -> None:
        if self._fail:
            raise httpx.ConnectError("boom")
        self.posted.append(base)


def _reembed_registry(**kwargs: Any) -> _ReembedRegistry:
    return _ReembedRegistry(
        mcp=_FakeMcp(),
        secrets=_FakeSecrets(),
        tenant="local",
        prefs=_FakeModulePrefs(),
        docker=None,
        **kwargs,
    )


async def test_reembed_fans_out_to_reindexable_modules() -> None:
    registry = _reembed_registry(manifest=_reindexable_manifest())
    result = await registry.reembed()
    assert registry.posted == ["http://echo:8080"]
    assert result == [{"module": "knowledge", "status": "started"}]


async def test_reembed_skips_non_reindexable_modules() -> None:
    registry = _reembed_registry(manifest=_echo_manifest())  # reindexable defaults to False
    result = await registry.reembed()
    assert registry.posted == []
    assert result == []


async def test_reembed_skips_unhealthy_modules() -> None:
    registry = _reembed_registry(manifest=_reindexable_manifest(), healthy=False)
    assert await registry.reembed() == []


async def test_reembed_reports_a_module_that_fails() -> None:
    registry = _reembed_registry(manifest=_reindexable_manifest(), fail=True)
    assert await registry.reembed() == [{"module": "knowledge", "status": "error"}]


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
    # No query params → the proxy GET carries no params kwarg (#209 helper).
    mock_client.get.assert_called_once_with("/pages/browse")


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
    # With a socket present the container is torn down now, so teardown is not deferred.
    assert result == {"removed": "echo", "containers": 1, "container_teardown_deferred": False}
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


async def test_remove_without_docker_soft_removes() -> None:
    # No socket: removal is decoupled from Docker (#382). It must NOT raise — the module is
    # tombstoned now (hidden everywhere, dropped from routing) and the container teardown is
    # deferred to the next startup reconcile.
    registry, _, _ = _registry(docker=None)
    result = await registry.remove("echo")
    assert result == {"removed": "echo", "containers": 0, "container_teardown_deferred": True}
    # Tombstone set → dropped from discovery and flagged on the snapshot.
    assert "echo" in await registry._prefs.removed_modules("local")
    snaps = await registry.snapshot()
    assert snaps[0].removed is True
    assert await registry.enabled_mcp_urls() == []


async def test_remove_protected_without_docker_is_403() -> None:
    # A protected name is rejected before tombstoning, even with no socket (#382): we must
    # never persist a removal for a core/data-plane service. (Configure the module as a
    # protected name to exercise the defensive denylist directly.)
    registry, _, _ = _registry(manifest=_protected_named_manifest(), docker=None)
    with pytest.raises(HTTPException) as err:
        await registry.remove("postgres")
    assert err.value.status_code == 403
    # Nothing was tombstoned.
    assert await registry._prefs.removed_modules("local") == set()


async def test_remove_protected_propagates_as_403() -> None:
    # ``echo`` is not in PROTECTED, so the denylist check is skipped and the Docker layer's
    # own DockerError (the real protected guard) surfaces as a 403.
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


async def test_reconcile_tombstones_logs_repr_of_bare_exception() -> None:
    # A bare TimeoutError() str()s to "" — logging str(exc) here would record an empty
    # error field. repr(exc) is always non-empty (#498, the #478/#482 pattern applied
    # to this handler's remaining bare-Exception catch). Tombstone first with a clean
    # docker so "echo" lands in removed_modules, then make the reconcile's own
    # remove_module call fail with a bare exception.
    docker = _FakeDocker(count=1)
    registry, _, _ = _registry(docker=docker)
    await registry.remove("echo")
    docker._error = TimeoutError()
    with capture_logs() as logs:
        await registry.reconcile_tombstones()
    failures = [entry for entry in logs if entry["event"] == "tombstone reconcile failed"]
    assert len(failures) == 1
    assert failures[0]["error"] == "TimeoutError()"


# ── Docker status (#622): proactive, accurate reporting — never "removal disabled" ─


def test_docker_status_available_has_no_reason() -> None:
    registry, _, _ = _registry(docker=_FakeDocker())
    assert registry.docker_status() == DockerStatus(available=True, reason=None)


def test_docker_status_unavailable_reports_the_captured_reason() -> None:
    registry, _, _ = _registry(docker=None, docker_reason="permission denied")
    assert registry.docker_status() == DockerStatus(available=False, reason="permission denied")


def test_docker_status_unavailable_with_no_reason_captured() -> None:
    # docker=None with no reason (e.g. a caller that never probed) still reports unavailable.
    registry, _, _ = _registry(docker=None)
    assert registry.docker_status() == DockerStatus(available=False, reason=None)


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


# ── Account/collection model (ADR-0030) ───────────────────────────────────────


def _collections_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="calendar",
        version="0.5.0",
        collections=CollectionsSpec(noun="calendar", multi=True, providers=["google"]),
    )


def _accounts_payload() -> dict[str, Any]:
    return {
        "noun": "calendar",
        "multi": True,
        "accounts": [
            {
                "account": "google",
                "provider": "google",
                "label": "Google",
                "connected": True,
                "collections": [
                    {"account": "google", "collection": "primary", "title": "Primary"},
                    {"account": "google", "collection": "work", "title": "Work"},
                ],
            }
        ],
    }


async def test_accounts_view_proxies_and_merges_prefs() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_collections_manifest())
    ref = CollectionRef(account="google", collection="primary")
    await registry.set_collections("calendar", CollectionPrefs(enabled=[ref], active=ref))

    mock_response = MagicMock()
    mock_response.json.return_value = _accounts_payload()
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        view = await registry.accounts_view("calendar")

    mock_client.get.assert_called_once_with("/accounts")
    cols = view.accounts[0].collections
    # The active+enabled collection is flagged; the untouched one is off.
    assert (cols[0].collection, cols[0].enabled, cols[0].active) == ("primary", True, True)
    assert (cols[1].collection, cols[1].enabled, cols[1].active) == ("work", False, False)


async def test_accounts_view_404_without_collections_spec() -> None:
    registry, _, _ = _registry()  # echo manifest declares no collections
    with pytest.raises(HTTPException) as err:
        await registry.accounts_view("echo")
    assert err.value.status_code == 404


async def test_set_collections_persists() -> None:
    registry, _, _ = _registry(manifest=_collections_manifest())
    ref = CollectionRef(account="google", collection="primary")
    await registry.set_collections("calendar", CollectionPrefs(enabled=[ref], active=ref))
    stored = await registry.collection_prefs("calendar")
    assert stored.active is not None
    assert stored.active.collection == "primary"


async def test_set_collections_rejects_active_not_enabled() -> None:
    registry, _, _ = _registry(manifest=_collections_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.set_collections(
            "calendar",
            CollectionPrefs(enabled=[], active=CollectionRef(account="google", collection="x")),
        )
    assert err.value.status_code == 400


async def test_set_collections_404_without_spec() -> None:
    registry, _, _ = _registry()  # echo, no collections
    with pytest.raises(HTTPException) as err:
        await registry.set_collections("echo", CollectionPrefs())
    assert err.value.status_code == 404


async def test_collection_prefs_default_empty_means_local() -> None:
    registry, _, _ = _registry(manifest=_collections_manifest())
    prefs = await registry.collection_prefs("calendar")
    assert prefs.enabled == []
    assert prefs.active is None


# ── Module docs proxy (#215) ──────────────────────────────────────────────────


def _docs_manifest() -> ModuleManifest:
    return ModuleManifest(name="echo", version="0.2.1", docs_url="/module-docs")


async def test_get_docs_proxies_module_docs_url() -> None:
    from unittest.mock import MagicMock

    registry, _, _ = _registry(manifest=_docs_manifest())
    docs_payload = {"documents": [{"path": "overview.md", "content": "# Echo"}]}

    mock_response = MagicMock()
    mock_response.json.return_value = docs_payload

    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await registry.get_docs("echo")

    assert result == docs_payload
    mock_client.get.assert_called_once_with("/module-docs")


async def test_get_docs_404_when_no_docs_url() -> None:
    registry, _, _ = _registry(manifest=_echo_manifest())  # echo manifest has no docs_url
    with pytest.raises(HTTPException) as err:
        await registry.get_docs("echo")
    assert err.value.status_code == 404


async def test_get_docs_404_for_unknown_module() -> None:
    registry, _, _ = _registry(manifest=_docs_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_docs("ghost")
    assert err.value.status_code == 404


# ── Per-tool enable/disable (#213) ────────────────────────────────────────────


def _tools_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="echo",
        version="0.1.0",
        tools=[
            ToolSpec(name="echo", input_schema={"type": "object"}),
            ToolSpec(name="echo_loud", input_schema={"type": "object"}),
        ],
    )


async def test_get_tool_enabled_defaults_true() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    assert await registry.get_tool_enabled("echo", "echo") is True


async def test_set_tool_enabled_false_and_re_read() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    await registry.set_tool_enabled("echo", "echo", False)
    assert await registry.get_tool_enabled("echo", "echo") is False
    assert await registry.get_tool_enabled("echo", "echo_loud") is True


async def test_re_enable_tool_restores() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    await registry.set_tool_enabled("echo", "echo", False)
    await registry.set_tool_enabled("echo", "echo", True)
    assert await registry.get_tool_enabled("echo", "echo") is True


async def test_set_tool_enabled_unknown_module_is_404() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.set_tool_enabled("ghost", "echo", False)
    assert err.value.status_code == 404


async def test_set_tool_enabled_unknown_tool_is_404() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.set_tool_enabled("echo", "rm_rf", False)
    assert err.value.status_code == 404


async def test_get_tool_enabled_unknown_tool_is_404() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.get_tool_enabled("echo", "rm_rf")
    assert err.value.status_code == 404


async def test_snapshot_includes_disabled_tools() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    await registry.set_tool_enabled("echo", "echo", False)
    snaps = await registry.snapshot()
    assert snaps[0].disabled_tools == ["echo"]


async def test_disabled_tools_set_returns_flat_union() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    await registry.set_tool_enabled("echo", "echo", False)
    disabled = await registry.disabled_tools_set()
    assert "echo" in disabled
    assert "echo_loud" not in disabled


async def test_disabled_tools_set_empty_when_all_enabled() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    assert await registry.disabled_tools_set() == set()


async def test_disabled_tools_set_excludes_disabled_module() -> None:
    registry, _, _ = _registry(manifest=_tools_manifest())
    await registry.set_tool_enabled("echo", "echo", False)
    await registry.set_enabled("echo", False)
    # A disabled module's tools are already excluded by the URL filter; they must not
    # also appear in the flat disabled set (the set is only for enabled modules).
    disabled = await registry.disabled_tools_set()
    assert disabled == set()


# ── Proxy hardening: a module error is a controlled status, not a raw 500 (#209) ──


def _patch_get(*, response: Any = None, error: Exception | None = None) -> Any:
    """A context manager patching the registry's httpx client GET (response or error)."""
    cls = patch("epicurus_core_app.modules.httpx.AsyncClient")
    client = AsyncMock()
    client.get = AsyncMock(side_effect=error) if error else AsyncMock(return_value=response)
    return cls, client


def _erroring_response(status: int) -> MagicMock:
    """A mock httpx response whose ``raise_for_status`` raises for *status*."""
    req = httpx.Request("GET", "http://module:8080/x")
    resp = MagicMock()
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            str(status), request=req, response=httpx.Response(status, request=req)
        )
    )
    return resp


async def test_get_status_unreachable_module_is_clean_502() -> None:
    registry, _, _ = _registry(manifest=_knowledge_manifest())
    cls, client = _patch_get(error=httpx.ConnectError("connection refused"))
    with cls as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(HTTPException) as err:
            await registry.get_status("knowledge")
    assert err.value.status_code == 502


async def test_get_status_module_5xx_is_502() -> None:
    registry, _, _ = _registry(manifest=_knowledge_manifest())
    cls, client = _patch_get(response=_erroring_response(500))
    with cls as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(HTTPException) as err:
            await registry.get_status("knowledge")
    assert err.value.status_code == 502


async def test_resolve_entity_passes_module_404_through() -> None:
    # A module's client-error (4xx) surfaces as-is — a missing entity stays a 404.
    registry, _, _ = _registry(manifest=_resolver_manifest())
    cls, client = _patch_get(response=_erroring_response(404))
    with cls as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(HTTPException) as err:
            await registry.resolve_entity("calendar", "event", "missing")
    assert err.value.status_code == 404


# ── Auto-connect / disconnect on OAuth (#209) ──────────────────────────────────


async def test_autoconnect_seeds_empty_selection() -> None:
    registry, _, _ = _registry(manifest=_collections_manifest())
    resp = MagicMock()
    resp.json.return_value = _accounts_payload()  # google connected: primary + work
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        seeded = await registry.autoconnect_collections("google")
    assert seeded == ["calendar"]
    prefs = await registry.collection_prefs("calendar")
    assert [r.collection for r in prefs.enabled] == ["primary", "work"]
    assert prefs.active is not None
    assert prefs.active.collection == "primary"  # first writable becomes active


async def test_autoconnect_never_overrides_existing_selection() -> None:
    registry, _, _ = _registry(manifest=_collections_manifest())
    ref = CollectionRef(account="google", collection="work")
    await registry.set_collections("calendar", CollectionPrefs(enabled=[ref], active=ref))
    # No httpx mock: it must short-circuit before fetching /accounts.
    assert await registry.autoconnect_collections("google") == []
    prefs = await registry.collection_prefs("calendar")
    assert prefs.active is not None
    assert prefs.active.collection == "work"


async def test_autoconnect_ignores_modules_not_using_provider() -> None:
    registry, _, _ = _registry()  # echo declares no collections
    assert await registry.autoconnect_collections("google") == []


async def test_autoconnect_logs_repr_of_bare_exception() -> None:
    # A bare TimeoutError() str()s to "" — logging str(exc) here would record an empty
    # error field. accounts_view's underlying GET raises before _get_json's own
    # httpx.HTTPError handling applies, so the bare exception reaches this handler's
    # except Exception directly (#498, the #478/#482 pattern applied to this handler's
    # remaining bare-Exception catch).
    registry, _, _ = _registry(manifest=_collections_manifest())
    cls, client = _patch_get(error=TimeoutError())
    with cls as mock_cls, capture_logs() as logs:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        seeded = await registry.autoconnect_collections("google")
    assert seeded == []
    failures = [entry for entry in logs if entry["event"] == "autoconnect: accounts unavailable"]
    assert len(failures) == 1
    assert failures[0]["error"] == "TimeoutError()"


async def test_disconnect_clears_provider_selection() -> None:
    registry, _, _ = _registry(manifest=_collections_manifest())
    g = CollectionRef(account="google", collection="primary")
    await registry.set_collections("calendar", CollectionPrefs(enabled=[g], active=g))
    assert await registry.disconnect_collections("google") == ["calendar"]
    prefs = await registry.collection_prefs("calendar")
    assert prefs.enabled == []
    assert prefs.active is None


# ── Cross-module pending-suggestions feed (#KB-refactor) ───────────────────────


def _review_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="knowledge",
        version="0.14.0",
        pages=[PageSpec(id="review", title="Suggestions", archetype="review")],
    )


async def test_all_suggestions_aggregates_review_pages() -> None:
    registry, _, _ = _registry(manifest=_review_manifest())
    resp = MagicMock()
    resp.json.return_value = {
        "title": "Suggestions",
        "suggestions": [
            {
                "id": "s1",
                "title": "a",
                "path": "kb/a.md",
                "operation": "update",
                "origin": "agent",
                "note": "",
                "created_at": "2026-06-24T00:00:00Z",
                "diff": "",
                "to_path": "",
                "current": "",
                "content": "",
            }
        ],
    }
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await registry.all_suggestions()
    assert len(result) == 1
    assert result[0]["module"] == "knowledge"
    assert result[0]["page_id"] == "review"
    assert result[0]["id"] == "s1"
    client.get.assert_called_once_with("/pages/review")


async def test_all_suggestions_empty_without_a_review_page() -> None:
    # A module with only a browser page contributes nothing to the feed.
    registry, _, _ = _registry(manifest=_pages_manifest())
    assert await registry.all_suggestions() == []


# ── Cross-module calendar-feed aggregate (#469) ─────────────────────────────────


async def test_calendar_feed_items_aggregates_and_stamps_module() -> None:
    registry, _, _ = _registry(manifest=ModuleManifest(name="tasks", version="0.15.3"))
    resp = MagicMock()
    resp.json.return_value = [
        {
            "id": "t1",
            "title": "Buy milk",
            "date": "2026-07-15",
            "status": "open",
            "ref_id": "t1",
            "kind": "task",
        },
    ]
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await registry.calendar_feed_items("2026-07-01", "2026-08-01")
    assert len(result) == 1
    assert result[0]["module"] == "tasks"
    assert result[0]["id"] == "t1"
    client.get.assert_called_once_with(
        "/calendar-feed", params={"start": "2026-07-01", "end": "2026-08-01"}
    )


async def test_calendar_feed_items_skips_a_module_that_404s() -> None:
    # Not a manifest-declared capability (unlike `resolver`/`attachable`) — a module with
    # no `/calendar-feed` route (e.g. calendar itself, or any module that never opted in)
    # 404s, and that 404 is the only signal; the feed degrades to "contributes nothing"
    # for that module rather than failing the whole aggregate (#469).
    registry, _, _ = _registry(manifest=ModuleManifest(name="calendar", version="0.16.0"))
    request = httpx.Request("GET", "http://echo:8080/calendar-feed")
    resp = httpx.Response(404, request=request)
    with patch("epicurus_core_app.modules.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError("404", request=request, response=resp)
        )
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await registry.calendar_feed_items("2026-07-01", "2026-08-01")
    assert result == []


async def test_calendar_feed_items_skips_unhealthy_module() -> None:
    registry, _, _ = _registry(
        healthy=False, manifest=ModuleManifest(name="tasks", version="0.15.3")
    )
    assert await registry.calendar_feed_items("2026-07-01", "2026-08-01") == []


# ── Suggestions review on/off toggle (#KB-refactor) ────────────────────────────


async def test_suggestions_enabled_defaults_true_and_round_trips() -> None:
    registry, _, _ = _registry(manifest=_review_manifest())
    assert await registry.get_suggestions_enabled("knowledge") is True  # default on
    await registry.set_suggestions_enabled("knowledge", False)
    assert await registry.get_suggestions_enabled("knowledge") is False


async def test_set_suggestions_enabled_unknown_module_is_404() -> None:
    registry, _, _ = _registry(manifest=_review_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.set_suggestions_enabled("ghost", False)
    assert err.value.status_code == 404


# ── Probe cache: TTL, single-flight, targeted resolve (#478) ──────────────────


def _snap(name: str, *, healthy: bool = True) -> ModuleSnapshot:
    return ModuleSnapshot(
        manifest=ModuleManifest(name=name, version="0.1.0"), status=ModuleStatus(healthy=healthy)
    )


class _CountingRegistry(ModuleRegistry):
    """A registry whose network probe is a canned per-base response with call counts
    recorded — exercises the probe cache's TTL/single-flight/targeting (#478), independent
    of the health-transition logging (covered separately against the real ``_probe``)."""

    def __init__(self, responses: dict[str, ModuleSnapshot], **kwargs: Any) -> None:
        super().__init__(list(responses.keys()), **kwargs)
        self._responses = responses
        self.probe_calls: list[str] = []

    async def _probe(self, base: str) -> ModuleSnapshot:
        self.probe_calls.append(base)
        return self._responses[base]


def _multi_registry(
    names: list[str], *, unhealthy: frozenset[str] = frozenset()
) -> _CountingRegistry:
    responses = {f"http://{n}:8080": _snap(n, healthy=n not in unhealthy) for n in names}
    return _CountingRegistry(
        responses,
        mcp=_FakeMcp(),
        secrets=_FakeSecrets(),
        tenant="local",
        prefs=_FakeModulePrefs(),
        docker=None,
    )


async def test_snapshot_probes_each_base_once_when_called_twice() -> None:
    # Two calls inside the same TTL window must not double the network cost — the whole
    # point of #478 (steady state used to re-probe the fleet on every routed call).
    registry = _multi_registry(["calendar", "tasks"])
    await registry.snapshot()
    await registry.snapshot()
    assert registry.probe_calls == ["http://calendar:8080", "http://tasks:8080"]


async def test_resolve_reuses_a_fresh_cache_entry() -> None:
    registry = _multi_registry(["calendar"])
    await registry._resolve("calendar")
    await registry._resolve("calendar")
    assert registry.probe_calls == ["http://calendar:8080"]


async def test_resolve_never_probes_a_different_modules_base() -> None:
    # Steady state (the fleet is already known): resolving one module must not fan out to
    # the rest — the core complaint in #478 ("a hung unrelated module no longer delays
    # other modules' calls"). The very first resolve ever is a documented exception (below):
    # it still has to learn the name→base map, so it legitimately probes the whole fleet once.
    registry = _multi_registry(["calendar", "tasks", "mail"])
    await registry.snapshot()  # warm the registry up first
    registry.probe_calls.clear()
    await registry._resolve("calendar")
    # Everything is fresh, so this costs zero network calls — a fortiori it never touches
    # tasks/mail's bases.
    assert registry.probe_calls == []


async def test_first_ever_resolve_learns_the_whole_fleet_once() -> None:
    # Documented cold-start exception: nothing is known yet, so resolving even one module
    # must probe every base to learn which is which — but only this once.
    registry = _multi_registry(["calendar", "tasks", "mail"])
    await registry._resolve("calendar")
    assert sorted(registry.probe_calls) == [
        "http://calendar:8080",
        "http://mail:8080",
        "http://tasks:8080",
    ]
    registry.probe_calls.clear()
    await registry._resolve("tasks")  # now warm — costs nothing beyond its own cache TTL
    assert registry.probe_calls == []


async def test_resolve_concurrent_calls_for_the_same_module_single_flight() -> None:
    registry = _multi_registry(["calendar"])
    await asyncio.gather(*(registry._resolve("calendar") for _ in range(8)))
    assert registry.probe_calls == ["http://calendar:8080"]


async def test_resolve_unknown_name_probes_only_the_unprobed_fleet_then_404s() -> None:
    registry = _multi_registry(["calendar", "tasks"])
    with pytest.raises(HTTPException) as err:
        await registry._resolve("ghost")
    assert err.value.status_code == 404
    assert sorted(registry.probe_calls) == ["http://calendar:8080", "http://tasks:8080"]


async def test_resolve_a_second_unknown_name_is_free_once_the_fleet_is_known() -> None:
    registry = _multi_registry(["calendar", "tasks"])
    await registry._resolve("calendar")  # learns tasks' base too as a side effect
    registry.probe_calls.clear()
    with pytest.raises(HTTPException):
        await registry._resolve("ghost")
    assert registry.probe_calls == []  # warm registry: a bad name costs zero network calls


async def test_snapshot_force_bypasses_the_cache() -> None:
    registry = _multi_registry(["calendar"])
    await registry.snapshot()
    await registry.snapshot(force=True)
    assert registry.probe_calls == ["http://calendar:8080", "http://calendar:8080"]


async def test_cache_expires_after_the_healthy_ttl() -> None:
    registry = _multi_registry(["calendar"])
    with patch("epicurus_core_app.modules.time.monotonic", return_value=1_000.0):
        await registry.snapshot()
    stale_at = 1_000.0 + ModuleRegistry._HEALTHY_PROBE_TTL_S + 0.1
    with patch("epicurus_core_app.modules.time.monotonic", return_value=stale_at):
        await registry.snapshot()
    assert registry.probe_calls == ["http://calendar:8080", "http://calendar:8080"]


async def test_unhealthy_cache_expires_sooner_than_healthy() -> None:
    registry = _multi_registry(["calendar"], unhealthy=frozenset({"calendar"}))
    assert ModuleRegistry._UNHEALTHY_PROBE_TTL_S < ModuleRegistry._HEALTHY_PROBE_TTL_S
    with patch("epicurus_core_app.modules.time.monotonic", return_value=1_000.0):
        await registry.snapshot()
    # Past the short unhealthy TTL but still inside the long healthy one — an unhealthy
    # entry must already be stale here so recovery is checked promptly (#478).
    past_unhealthy_ttl = 1_000.0 + ModuleRegistry._UNHEALTHY_PROBE_TTL_S + 0.1
    with patch("epicurus_core_app.modules.time.monotonic", return_value=past_unhealthy_ttl):
        await registry.snapshot()
    assert registry.probe_calls == ["http://calendar:8080", "http://calendar:8080"]


async def test_prefs_overlay_is_never_stale_even_when_the_probe_cache_is_fresh() -> None:
    """The enabled/removed/disabled_tools overlay must reflect current prefs on every call,
    even when the underlying network probe is served from cache — only the probe itself
    is time-cached, never the operator's settings (#478)."""
    registry = _multi_registry(["calendar"])
    await registry.snapshot()  # populates + caches the probe
    await registry.set_enabled("calendar", False)
    snaps = await registry.snapshot()  # same TTL window — probe cache still fresh
    assert registry.probe_calls == ["http://calendar:8080"]  # no re-probe...
    assert snaps[0].enabled is False  # ...but the prefs flag is still live


# ── Health-transition logging (#478): WARN/INFO on change, DEBUG at boot, no repeats ──


def _real_probe_registry(base: str = "http://echo:8080") -> ModuleRegistry:
    """A plain registry with the real (unstubbed) ``_probe`` — the HTTP layer is mocked
    per test instead, so transition logging genuinely runs."""
    return ModuleRegistry(
        [base], mcp=_FakeMcp(), secrets=_FakeSecrets(), tenant="local", prefs=_FakeModulePrefs()
    )


def _probe_client(manifest: ModuleManifest, *, health_status: int = 200) -> tuple[Any, AsyncMock]:
    """A mocked ``httpx.AsyncClient`` answering ``/manifest`` then ``/health`` in order."""
    manifest_resp = MagicMock()
    manifest_resp.raise_for_status = MagicMock()
    manifest_resp.json.return_value = manifest.model_dump(mode="json")
    health_resp = MagicMock()
    health_resp.status_code = health_status
    health_resp.json.return_value = {"version": manifest.version}
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[manifest_resp, health_resp])
    return patch("epicurus_core_app.modules.httpx.AsyncClient"), client


async def test_probe_failure_logs_debug_while_never_yet_healthy() -> None:
    # The startup grace window: a module that has never come up yet must not WARN.
    registry = _real_probe_registry()
    cls, client = _patch_get(error=httpx.ConnectError("boom"))
    with cls as mock_cls, capture_logs() as logs:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await registry._probe("http://echo:8080")
    failures = [entry for entry in logs if entry["event"] == "module probe failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "debug"
    assert failures[0]["error"]  # non-empty — the #478 bug was str(exc) == "" for TimeoutError


async def test_probe_transition_healthy_to_unreachable_logs_warn() -> None:
    registry = _real_probe_registry()
    ok_cls, ok_client = _probe_client(_echo_manifest())
    with ok_cls as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=ok_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        first = await registry._probe("http://echo:8080")
    assert first.status.healthy is True

    fail_cls, fail_client = _patch_get(error=httpx.ConnectError("boom"))
    with fail_cls as mock_cls, capture_logs() as logs:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=fail_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        second = await registry._probe("http://echo:8080")
    assert second.status.healthy is False
    failures = [entry for entry in logs if entry["event"] == "module probe failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "warning"
    assert failures[0]["error"]


async def test_probe_transition_unreachable_to_healthy_logs_info() -> None:
    registry = _real_probe_registry()
    fail_cls, fail_client = _patch_get(error=httpx.ConnectError("boom"))
    with fail_cls as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=fail_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await registry._probe("http://echo:8080")  # never-healthy yet: debug, sets prev=False

    ok_cls, ok_client = _probe_client(_echo_manifest())
    with ok_cls as mock_cls, capture_logs() as logs:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=ok_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        recovered = await registry._probe("http://echo:8080")
    assert recovered.status.healthy is True
    recoveries = [entry for entry in logs if entry["event"] == "module recovered"]
    assert len(recoveries) == 1
    assert recoveries[0]["log_level"] == "info"
    assert recoveries[0]["module"] == "echo"


async def test_probe_steady_state_unreachable_does_not_repeat_the_warning() -> None:
    registry = _real_probe_registry()
    cls1, client1 = _patch_get(error=httpx.ConnectError("boom"))
    with cls1 as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client1)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await registry._probe("http://echo:8080")  # 1st: never-healthy → debug

    cls2, client2 = _patch_get(error=httpx.ConnectError("boom"))
    with cls2 as mock_cls, capture_logs() as logs:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client2)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await registry._probe("http://echo:8080")  # 2nd: still down, steady-state → silent

    assert [entry for entry in logs if entry["event"] == "module probe failed"] == []


async def test_probe_unhealthy_status_without_exception_has_a_real_reason() -> None:
    # Manifest fetch succeeds but /health reports non-200 — no exception, but still an
    # unhealthy transition that deserves a meaningful (not empty) reason.
    registry = _real_probe_registry()
    cls, client = _probe_client(_echo_manifest(), health_status=503)
    with cls as mock_cls, capture_logs() as logs:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        snap = await registry._probe("http://echo:8080")
    assert snap.status.healthy is False
    assert snap.status.version is None
    failures = [entry for entry in logs if entry["event"] == "module probe failed"]
    assert failures[0]["error"] == "health check returned 503"


# ── the reserved "core" pseudo-module (ADR-0093 §2) ──────────────────────────


class _FakeCore:
    """A stand-in :class:`CorePseudoModule` — records dispatch, needs no DB and no HTTP."""

    def __init__(self, *, name: str = "core", suggestions: list[dict[str, Any]] | None = None):
        self._name = name
        self._suggestions = suggestions if suggestions is not None else []
        self.actions: list[tuple[str, str, str, str | None]] = []
        self.audits: list[tuple[str, int]] = []

    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            name=self._name,
            version="9.9.9",
            pages=[PageSpec(id="playbooks", title="Playbooks", archetype="review")],
            ui=UiSection(icon="book-open"),
        )

    async def get_page(self, page_id: str) -> dict[str, Any]:
        if page_id != "playbooks":
            raise HTTPException(status_code=404, detail="no such page")
        return {"title": "Playbooks", "suggestions": self._suggestions}

    async def review_action(
        self, page_id: str, suggestion_id: str, action: str, content: str | None = None
    ) -> dict[str, Any]:
        self.actions.append((page_id, suggestion_id, action, content))
        return {"id": suggestion_id, "status": f"{action}d"}

    async def review_audit(self, page_id: str, *, limit: int = 50) -> dict[str, Any]:
        self.audits.append((page_id, limit))
        return {"decisions": []}


def _registry_with_core(
    core: _FakeCore | None = None,
) -> tuple[_StubRegistry, _FakeCore]:
    the_core = core or _FakeCore()
    mcp, secrets = _FakeMcp(), _FakeSecrets()
    registry = _StubRegistry(  # type: ignore[arg-type]
        mcp=mcp,
        secrets=secrets,
        tenant="local",
        prefs=_FakeModulePrefs(),
        core=the_core,
    )
    return registry, the_core


async def test_core_snapshot_is_healthy_and_enabled() -> None:
    """It *is* this process — a probe would ask whether the core is up while it answers."""
    registry, _ = _registry_with_core()
    snap = registry.core_snapshot()
    assert snap is not None
    assert snap.manifest.name == "core"
    assert snap.status.healthy is True
    assert snap.enabled is True
    assert snap.removed is False


async def test_core_snapshot_is_none_when_unwired() -> None:
    registry, _, _ = _registry()
    assert registry.core_snapshot() is None


async def test_core_is_absent_from_snapshot_so_it_stays_1to1_with_bases() -> None:
    """The invariant several callers zip against with strict=True must survive the pseudo-module."""
    registry, _ = _registry_with_core()
    snaps = await registry.snapshot()
    assert len(snaps) == 1  # exactly the one configured base
    assert [s.manifest.name for s in snaps] == ["echo"]


async def test_core_never_reaches_the_mcp_tool_surface() -> None:
    """A pseudo-module has no base, so it cannot leak into the agent's tool discovery."""
    registry, _ = _registry_with_core()
    urls = await registry.enabled_mcp_urls()
    assert urls == ["http://echo:8080/mcp"]
    assert not any("core" in u for u in urls)


async def test_core_never_reaches_the_reembed_fanout() -> None:
    registry, _ = _registry_with_core()
    assert await registry.reembed() == []  # echo isn't reindexable; core isn't reachable at all


async def test_get_page_dispatches_to_core_in_process() -> None:
    registry, _ = _registry_with_core()
    data = await registry.get_page("core", "playbooks")
    assert data["title"] == "Playbooks"


async def test_get_page_for_an_unknown_core_page_404s() -> None:
    registry, _ = _registry_with_core()
    with pytest.raises(HTTPException) as err:
        await registry.get_page("core", "ghost")
    assert err.value.status_code == 404


async def test_review_action_dispatches_to_core_with_the_edited_content() -> None:
    registry, core = _registry_with_core()
    out = await registry.review_action("core", "playbooks", "sid1", "approve", "edited")
    assert out["status"] == "approved"
    assert core.actions == [("playbooks", "sid1", "approve", "edited")]


async def test_review_audit_dispatches_to_core() -> None:
    registry, core = _registry_with_core()
    await registry.review_audit("core", "playbooks", limit=7)
    assert core.audits == [("playbooks", 7)]


async def test_all_suggestions_includes_the_core_queue() -> None:
    registry, _ = _registry_with_core(
        _FakeCore(suggestions=[{"id": "s1", "path": "instructions", "operation": "update"}])
    )
    items = await registry.all_suggestions()
    assert items == [
        {
            "id": "s1",
            "path": "instructions",
            "operation": "update",
            "module": "core",
            "page_id": "playbooks",
        }
    ]


async def test_all_suggestions_tolerates_a_broken_core_page() -> None:
    """A failing core page must not empty the whole cross-module inbox."""

    class _Broken(_FakeCore):
        async def get_page(self, page_id: str) -> dict[str, Any]:
            raise HTTPException(status_code=500, detail="boom")

    registry, _ = _registry_with_core(_Broken())
    assert await registry.all_suggestions() == []


async def test_all_suggestions_without_a_core_is_unchanged() -> None:
    registry, _, _ = _registry()
    assert await registry.all_suggestions() == []


async def test_core_cannot_be_disabled() -> None:
    registry, _ = _registry_with_core()
    with pytest.raises(HTTPException) as err:
        await registry.set_enabled("core", False)
    assert err.value.status_code == 403


async def test_core_cannot_be_removed() -> None:
    registry, _ = _registry_with_core()
    with pytest.raises(HTTPException) as err:
        await registry.remove("core")
    assert err.value.status_code == 403


async def test_core_review_cannot_be_switched_off() -> None:
    """ADR-0093's hard invariant: nothing self-applies, so review is mandatory for core."""
    registry, _ = _registry_with_core()
    with pytest.raises(HTTPException) as err:
        await registry.set_suggestions_enabled("core", False)
    assert err.value.status_code == 403


async def test_a_real_module_named_core_does_not_shadow_the_pseudo_module() -> None:
    """The name is reserved: dispatch goes in-process, never to a module that claims it."""
    mcp, secrets = _FakeMcp(), _FakeSecrets()
    registry = _StubRegistry(  # type: ignore[arg-type]
        manifest=ModuleManifest(
            name="core",
            version="0.0.1",
            pages=[PageSpec(id="playbooks", title="Impostor", archetype="review")],
        ),
        mcp=mcp,
        secrets=secrets,
        tenant="local",
        prefs=_FakeModulePrefs(),
        core=_FakeCore(),
    )
    data = await registry.get_page("core", "playbooks")
    assert data["title"] == "Playbooks"  # the in-process page, not the impostor's
