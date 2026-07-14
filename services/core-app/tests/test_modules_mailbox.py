"""Tests for the mailbox proxy methods on ModuleRegistry (ADR-0087, #550).

The mailbox send + attachment proxies are archetype-gated (mailbox-only) and forward to the
module; these stub httpx to avoid real HTTP, mirroring test_modules_file_tree.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from epicurus_core import ModuleManifest, PageSpec, SecretError
from epicurus_core_app.modules import ModuleRegistry, ModuleSnapshot, ModuleStatus


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

    async def removed_modules(self, tenant: str) -> set[str]:
        return set()

    async def get_disabled_tools(self, tenant: str, module: str) -> set[str]:
        return set()


def _mailbox_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="mail",
        version="0.10.0",
        pages=[PageSpec(id="mailbox", title="Mail", archetype="mailbox")],
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


def _post_ctx(response_data: dict[str, Any]) -> tuple[Any, Any]:
    """A context-manager AsyncClient whose ``post`` returns *response_data* (for _post_json)."""
    resp = MagicMock()
    resp.json.return_value = response_data
    resp.status_code = 200
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


# ── send_page_message ──────────────────────────────────────────────────────────


async def test_send_page_message_forwards_to_module() -> None:
    registry = _registry(_mailbox_manifest())
    ctx, client = _post_ctx({"id": "sent-1"})
    payload = {"body": "hi", "to": "a@x.com", "subject": "Hi", "reply_to_message_id": None}

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.send_page_message("mail", "mailbox", payload)

    assert result == {"id": "sent-1"}
    client.post.assert_called_once_with("/pages/mailbox/send", json=payload)


async def test_send_page_message_404_for_non_mailbox() -> None:
    registry = _registry(_browser_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.send_page_message("files", "browse", {"body": "x"})
    assert err.value.status_code == 404


async def test_send_page_message_404_for_unknown_module() -> None:
    registry = _registry(_mailbox_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.send_page_message("ghost", "mailbox", {"body": "x"})
    assert err.value.status_code == 404


# ── mark_page_thread_read ──────────────────────────────────────────────────────


async def test_mark_page_thread_read_forwards_to_module() -> None:
    registry = _registry(_mailbox_manifest())
    ctx, client = _post_ctx({"thread_id": "t1", "marked": 2})
    payload = {"thread_id": "t1", "message_ids": ["m1", "m2"]}

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=ctx):
        result = await registry.mark_page_thread_read("mail", "mailbox", payload)

    assert result == {"thread_id": "t1", "marked": 2}
    client.post.assert_called_once_with("/pages/mailbox/mark-read", json=payload)


async def test_mark_page_thread_read_404_for_non_mailbox() -> None:
    registry = _registry(_browser_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.mark_page_thread_read(
            "files", "browse", {"thread_id": "t1", "message_ids": []}
        )
    assert err.value.status_code == 404


async def test_mark_page_thread_read_404_for_unknown_module() -> None:
    registry = _registry(_mailbox_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.mark_page_thread_read(
            "ghost", "mailbox", {"thread_id": "t1", "message_ids": []}
        )
    assert err.value.status_code == 404


# ── download_page_attachment ─────────────────────────────────────────────────────


async def test_download_page_attachment_forwards_and_returns_response() -> None:
    registry = _registry(_mailbox_manifest())
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)

    with patch("epicurus_core_app.modules.httpx.AsyncClient", return_value=client):
        out = await registry.download_page_attachment("mail", "mailbox", "m1", "att1")

    assert out is resp
    client.get.assert_called_once_with(
        "/pages/mailbox/attachment", params={"message_id": "m1", "attachment_id": "att1"}
    )


async def test_download_page_attachment_404_for_non_mailbox() -> None:
    registry = _registry(_browser_manifest())
    with pytest.raises(HTTPException) as err:
        await registry.download_page_attachment("files", "browse", "m1", "att1")
    assert err.value.status_code == 404
