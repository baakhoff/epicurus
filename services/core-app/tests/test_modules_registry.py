"""Unit tests for the module registry — module probing is stubbed in-process."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from epicurus_core import ModuleManifest, SecretError, ToolSpec, UiAction, UiSection
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
