"""The chat-bridge admin surface — the core's connect/manage path for the messaging module (#369).

The browser never holds a bot token (constraint #6) and a module is stateless w.r.t. identity
(constraint #4), so the **core** owns connecting a bridge: it writes the per-tenant token to
OpenBao (``messaging/<bridge>`` → ``{token, enabled}``) and then triggers the module's reload
control path so the bridge connects at runtime, no restart (ADR-0062). Reading status is a
straight proxy of the module's ``/status`` bridges list.

The web surface (#369) drives four operations per bridge: list (status), connect (store a
token), enable/disable (on/off, token kept), and disconnect (clear the token).
"""

from __future__ import annotations

import re
from contextlib import suppress
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from epicurus_core import SecretError, SecretStore, get_logger

log = get_logger("epicurus_core_app.messaging.bridges")

MESSAGING_MODULE = "messaging"
# A bridge id is interpolated into an OpenBao path and the module's reload URL — keep it to a
# safe, predictable shape so neither can be escaped (defence-in-depth, cf. modules._safe_segment).
_BRIDGE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _validate(bridge: str) -> str:
    if not _BRIDGE_RE.fullmatch(bridge):
        raise HTTPException(status_code=400, detail=f"invalid bridge id: {bridge!r}")
    return bridge


class SetTokenBody(BaseModel):
    """Body for connecting a bridge — the write-only bot token (never read back)."""

    token: str


class SetEnabledBody(BaseModel):
    """Body for the on/off toggle (the token is kept while disabled)."""

    enabled: bool


class BridgeModuleClient(Protocol):
    """The slice of the messaging module the admin drives: read status, reload one bridge.

    Kept a Protocol so the admin is unit-tested without a live module (the concrete
    :class:`RegistryBridgeClient` proxies through the module registry).
    """

    async def bridges(self) -> list[dict[str, Any]]: ...

    async def reload(self, bridge: str) -> dict[str, Any]: ...


class RegistryBridgeClient:
    """Talks to the messaging module through the module registry (the core's sole gateway)."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def bridges(self) -> list[dict[str, Any]]:
        status = await self._registry.get_status(MESSAGING_MODULE)
        bridges = status.get("bridges", []) if isinstance(status, dict) else []
        return list(bridges or [])

    async def reload(self, bridge: str) -> dict[str, Any]:
        """POST the module's ``/bridges/{bridge}/reload`` control path; return its fresh status."""
        base = await self._registry.base_url(MESSAGING_MODULE)
        try:
            async with httpx.AsyncClient(base_url=base, timeout=15) as client:
                resp = await client.post(f"/bridges/{bridge}/reload")
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                return data
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise HTTPException(
                status_code=code if 400 <= code < 500 else 502,
                detail=f"bridge reload failed: module returned {code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail="bridge reload failed: module unreachable"
            ) from exc


class BridgeAdmin:
    """Connect/manage bridges: token vault writes + the module reload, per tenant (ADR-0062)."""

    def __init__(self, secrets: SecretStore, client: BridgeModuleClient, *, tenant: str) -> None:
        self._secrets = secrets
        self._client = client
        self._tenant = tenant

    async def list_bridges(self) -> list[dict[str, Any]]:
        """Every bridge the module reports, with live connect/enabled/connected state."""
        return await self._client.bridges()

    async def _require_manageable(self, bridge: str) -> None:
        """404 unless ``bridge`` is a manageable bridge the module actually offers.

        Guards against writing a token for an unknown bridge or the in-process loopback
        (``manageable=False``), which the operator cannot connect.
        """
        names = {b.get("bridge") for b in await self._client.bridges() if b.get("manageable")}
        if bridge not in names:
            raise HTTPException(status_code=404, detail=f"no manageable bridge named {bridge!r}")

    def _path(self, bridge: str) -> str:
        return f"messaging/{bridge}"

    async def set_token(self, bridge: str, token: str) -> dict[str, Any]:
        """Connect a bridge: store its token (enabled on) and reload so it connects now."""
        _validate(bridge)
        token = token.strip()
        if not token:
            raise HTTPException(status_code=400, detail="token must not be empty")
        await self._require_manageable(bridge)
        await self._secrets.set(self._path(bridge), {"token": token, "enabled": True}, self._tenant)
        log.info("bridge connected", bridge=bridge)
        return await self._client.reload(bridge)

    async def set_enabled(self, bridge: str, enabled: bool) -> dict[str, Any]:
        """Turn a connected bridge on/off, keeping its stored token; reload to apply."""
        _validate(bridge)
        await self._require_manageable(bridge)
        try:
            data = await self._secrets.get(self._path(bridge), self._tenant)
        except SecretError:
            data = {}
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise HTTPException(
                status_code=400, detail=f"bridge {bridge!r} has no token; connect it first"
            )
        await self._secrets.set(
            self._path(bridge), {"token": token, "enabled": enabled}, self._tenant
        )
        log.info("bridge enabled toggled", bridge=bridge, enabled=enabled)
        return await self._client.reload(bridge)

    async def disconnect(self, bridge: str) -> dict[str, Any]:
        """Disconnect a bridge: clear its token from the vault and reload so it stops."""
        _validate(bridge)
        await self._require_manageable(bridge)
        with suppress(SecretError):  # already absent — disconnect is idempotent
            await self._secrets.delete(self._path(bridge), self._tenant)
        log.info("bridge disconnected", bridge=bridge)
        return await self._client.reload(bridge)


def create_messaging_router(admin: BridgeAdmin) -> APIRouter:
    """The ``/platform/v1/messaging`` surface the web shell renders (#369)."""
    router = APIRouter(prefix="/platform/v1/messaging", tags=["messaging"])

    @router.get("/bridges", response_model=list[dict[str, Any]])
    async def list_bridges() -> list[dict[str, Any]]:
        """Every bridge + its live state (connect/enabled/connected), for the connect surface."""
        return await admin.list_bridges()

    @router.put("/bridges/{bridge}/token", response_model=dict)
    async def set_bridge_token(bridge: str, body: SetTokenBody) -> dict[str, Any]:
        """Connect a bridge by storing its bot token (write-only) and connecting it now."""
        return await admin.set_token(bridge, body.token)

    @router.post("/bridges/{bridge}/enabled", response_model=dict)
    async def set_bridge_enabled(bridge: str, body: SetEnabledBody) -> dict[str, Any]:
        """Turn a connected bridge on or off without forgetting its token."""
        return await admin.set_enabled(bridge, body.enabled)

    @router.delete("/bridges/{bridge}", response_model=dict)
    async def disconnect_bridge(bridge: str) -> dict[str, Any]:
        """Disconnect a bridge — clears its stored token; it stops receiving/sending."""
        return await admin.disconnect(bridge)

    return router
