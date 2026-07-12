"""Tests for the review-queue proxy methods on ModuleRegistry (#220, ADR-0033).

Each test stubs out httpx.AsyncClient to avoid real HTTP, mirroring
test_modules_file_tree.py.
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


def _review_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="knowledge",
        version="0.10.0",
        pages=[
            PageSpec(id="vault", title="Knowledge", archetype="editor"),
            PageSpec(id="review", title="Suggestions", archetype="review"),
        ],
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


def _mock_client(response_data: dict[str, Any]) -> Any:
    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_client


def _mock_get_client(response_data: dict[str, Any]) -> Any:
    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_client


# ── review_action ───────────────────────────────────────────────────────────────


async def test_approve_proxies_post_to_module() -> None:
    registry = _registry(_review_manifest())
    ctx, mock_client = _mock_client({"id": "abc", "status": "approved"})

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.review_action("knowledge", "review", "abc123", "approve")

    assert result == {"id": "abc", "status": "approved"}
    mock_client.post.assert_called_once_with("/pages/review/suggestions/abc123/approve")


async def test_reject_proxies_post_to_module() -> None:
    registry = _registry(_review_manifest())
    ctx, mock_client = _mock_client({"id": "abc", "status": "rejected"})

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.review_action("knowledge", "review", "abc123", "reject")

    assert result["status"] == "rejected"
    mock_client.post.assert_called_once_with("/pages/review/suggestions/abc123/reject")


async def test_review_action_404_for_non_review_page() -> None:
    registry = _registry(_review_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.review_action("knowledge", "vault", "abc", "approve")
    assert err.value.status_code == 404


async def test_review_action_404_for_unknown_module() -> None:
    registry = _registry(_review_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.review_action("ghost", "review", "abc", "approve")
    assert err.value.status_code == 404


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "with/slash"])
async def test_review_action_rejects_unsafe_suggestion_id(bad: str) -> None:
    registry = _registry(_review_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.review_action("knowledge", "review", bad, "approve")
    assert err.value.status_code == 400


# ── review_audit (ADR-0090, #542) ──────────────────────────────────────────────


async def test_review_audit_proxies_get_to_module() -> None:
    registry = _registry(_review_manifest())
    ctx, mock_client = _mock_get_client(
        {"decisions": [{"id": "abc", "decision": "approved", "path": "a.md"}]}
    )

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.review_audit("knowledge", "review")

    assert result == {"decisions": [{"id": "abc", "decision": "approved", "path": "a.md"}]}
    mock_client.get.assert_called_once_with("/pages/review/audit", params={"limit": "50"})


async def test_review_audit_forwards_limit() -> None:
    registry = _registry(_review_manifest())
    ctx, mock_client = _mock_get_client({"decisions": []})

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        await registry.review_audit("knowledge", "review", limit=10)

    mock_client.get.assert_called_once_with("/pages/review/audit", params={"limit": "10"})


async def test_review_audit_404_for_non_review_page() -> None:
    registry = _registry(_review_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.review_audit("knowledge", "vault")
    assert err.value.status_code == 404


async def test_review_audit_404_for_unknown_module() -> None:
    registry = _registry(_review_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.review_audit("ghost", "review")
    assert err.value.status_code == 404
