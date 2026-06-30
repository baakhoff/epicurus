"""Unit tests for the chat-bridge admin (#369, ADR-0062): token vault writes + module reload,
and the ``/platform/v1/messaging`` router. No live module — a fake client stands in for it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from epicurus_core import SecretError
from epicurus_core_app.messaging.bridges import (
    BridgeAdmin,
    RegistryBridgeClient,
    create_messaging_router,
)


class _FakeSecrets:
    """In-memory SecretStore stand-in keyed by (path, tenant)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str | None], dict[str, Any]] = {}

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        self.store[(path, tenant_id)] = data

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if (path, tenant_id) not in self.store:
            raise SecretError(f"no secret at {path}")
        return self.store[(path, tenant_id)]

    async def delete(self, path: str, tenant_id: str | None = None) -> None:
        if (path, tenant_id) not in self.store:
            raise SecretError(f"no secret at {path}")
        del self.store[(path, tenant_id)]


class _FakeClient:
    """A fake messaging module: a configurable bridge list + a recording reload."""

    def __init__(self, bridges: list[dict[str, Any]] | None = None) -> None:
        self._bridges = (
            bridges
            if bridges is not None
            else [
                {"bridge": "discord", "label": "Discord", "manageable": True, "configured": False},
                {"bridge": "loopback", "label": "Loopback", "manageable": False},
            ]
        )
        self.reloaded: list[str] = []

    async def bridges(self) -> list[dict[str, Any]]:
        return self._bridges

    async def reload(self, bridge: str) -> dict[str, Any]:
        self.reloaded.append(bridge)
        return {"bridge": bridge, "connected": True}


def _admin(secrets: _FakeSecrets, client: _FakeClient) -> BridgeAdmin:
    return BridgeAdmin(secrets, client, tenant="local")  # type: ignore[arg-type]


# ── BridgeAdmin ──────────────────────────────────────────────────────────────────────────
async def test_set_token_writes_secret_and_reloads() -> None:
    secrets, client = _FakeSecrets(), _FakeClient()
    admin = _admin(secrets, client)
    result = await admin.set_token("discord", "  bot-tok  ")
    assert secrets.store[("messaging/discord", "local")] == {"token": "bot-tok", "enabled": True}
    assert client.reloaded == ["discord"]
    assert result["bridge"] == "discord"


async def test_set_token_rejects_blank() -> None:
    admin = _admin(_FakeSecrets(), _FakeClient())
    with pytest.raises(HTTPException) as exc:
        await admin.set_token("discord", "   ")
    assert exc.value.status_code == 400


async def test_set_token_rejects_unmanageable_bridge() -> None:
    admin = _admin(_FakeSecrets(), _FakeClient())
    # loopback is reported but not manageable → 404
    with pytest.raises(HTTPException) as exc:
        await admin.set_token("loopback", "tok")
    assert exc.value.status_code == 404


async def test_set_token_rejects_unknown_bridge() -> None:
    admin = _admin(_FakeSecrets(), _FakeClient())
    with pytest.raises(HTTPException) as exc:
        await admin.set_token("slack", "tok")
    assert exc.value.status_code == 404


async def test_set_token_rejects_invalid_id() -> None:
    admin = _admin(_FakeSecrets(), _FakeClient())
    with pytest.raises(HTTPException) as exc:
        await admin.set_token("../escape", "tok")
    assert exc.value.status_code == 400


async def test_set_enabled_keeps_token_and_reloads() -> None:
    secrets, client = _FakeSecrets(), _FakeClient()
    secrets.store[("messaging/discord", "local")] = {"token": "tok", "enabled": True}
    admin = _admin(secrets, client)
    await admin.set_enabled("discord", False)
    assert secrets.store[("messaging/discord", "local")] == {"token": "tok", "enabled": False}
    assert client.reloaded == ["discord"]


async def test_set_enabled_without_token_is_400() -> None:
    admin = _admin(_FakeSecrets(), _FakeClient())
    with pytest.raises(HTTPException) as exc:
        await admin.set_enabled("discord", True)
    assert exc.value.status_code == 400


async def test_disconnect_deletes_token_and_reloads() -> None:
    secrets, client = _FakeSecrets(), _FakeClient()
    secrets.store[("messaging/discord", "local")] = {"token": "tok", "enabled": True}
    admin = _admin(secrets, client)
    await admin.disconnect("discord")
    assert ("messaging/discord", "local") not in secrets.store
    assert client.reloaded == ["discord"]


async def test_disconnect_is_idempotent_when_absent() -> None:
    secrets, client = _FakeSecrets(), _FakeClient()
    admin = _admin(secrets, client)
    await admin.disconnect("discord")  # no stored token — must not raise
    assert client.reloaded == ["discord"]


async def test_list_bridges_passes_through() -> None:
    client = _FakeClient()
    admin = _admin(_FakeSecrets(), client)
    bridges = await admin.list_bridges()
    assert {b["bridge"] for b in bridges} == {"discord", "loopback"}


# ── RegistryBridgeClient.bridges (status extraction) ─────────────────────────────────────
class _FakeRegistry:
    def __init__(self, status: dict[str, Any]) -> None:
        self._status = status

    async def get_status(self, name: str) -> dict[str, Any]:
        return self._status


async def test_registry_client_extracts_bridges_from_status() -> None:
    reg = _FakeRegistry({"bridges": [{"bridge": "discord"}], "inbound_subject": "x"})
    client = RegistryBridgeClient(reg)
    assert await client.bridges() == [{"bridge": "discord"}]


async def test_registry_client_handles_missing_bridges_key() -> None:
    client = RegistryBridgeClient(_FakeRegistry({}))
    assert await client.bridges() == []


# ── router wiring ────────────────────────────────────────────────────────────────────────
def _router_app(secrets: _FakeSecrets, client: _FakeClient) -> FastAPI:
    app = FastAPI()
    app.include_router(create_messaging_router(_admin(secrets, client)))
    return app


def test_router_get_put_post_delete() -> None:
    secrets, client = _FakeSecrets(), _FakeClient()
    with TestClient(_router_app(secrets, client)) as http:
        listed = http.get("/platform/v1/messaging/bridges")
        assert listed.status_code == 200
        assert {b["bridge"] for b in listed.json()} == {"discord", "loopback"}

        connected = http.put("/platform/v1/messaging/bridges/discord/token", json={"token": "tok"})
        assert connected.status_code == 200
        assert secrets.store[("messaging/discord", "local")]["token"] == "tok"

        toggled = http.post(
            "/platform/v1/messaging/bridges/discord/enabled", json={"enabled": False}
        )
        assert toggled.status_code == 200
        assert secrets.store[("messaging/discord", "local")]["enabled"] is False

        removed = http.delete("/platform/v1/messaging/bridges/discord")
        assert removed.status_code == 200
        assert ("messaging/discord", "local") not in secrets.store
