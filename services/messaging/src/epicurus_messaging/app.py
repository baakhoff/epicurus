"""Runnable messaging service: ops endpoints + manifest, with the bridges wired to NATS.

It connects the :class:`~epicurus_messaging.manager.BridgeManager` to the inbox contract: a
message any bridge receives is published on ``messaging.inbound`` (the core runs the turn), and
each ``messaging.outbound`` reply is dispatched to the bridge it belongs to. The module never
calls an LLM (constraint #8).

The outbound subscription is under the configured default tenant (single-tenant v1, mirroring
the core's inbound consumer); multi-tenant fan-out is the same follow-up noted in ADR-0058.
``POST /bridges/{bridge}/reload`` is the core's control path: after the operator stores or
clears a bridge's token, the core calls it so the bridge connects/disconnects at runtime with
no module restart (ADR-0062).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    Event,
    EventBus,
    InboundMessage,
    OutboundMessage,
    SecretStore,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_messaging.service import MODULE_NAME, build_bridges, build_module
from epicurus_messaging.settings import MessagingSettings


def _service_version() -> str:
    """The installed distribution version, for ``/health``."""
    try:
        return pkg_version("epicurus-messaging")
    except PackageNotFoundError:
        return "0.0.0"


class _LoopbackInject(BaseModel):
    """Body for ``POST /loopback/inject`` — originate a message as if a user sent it."""

    text: str
    channel_id: str = "loopback"
    thread_id: str | None = None
    tenant: str | None = None  # defaults to the module's configured tenant
    sender_id: str = ""
    sender_name: str = ""


def create_app() -> FastAPI:
    settings = MessagingSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)
    tenant = settings.default_tenant_id
    secrets = SecretStore.from_settings(settings)
    manager = build_bridges(settings, secrets)
    module = build_module(manager)
    bus = EventBus.from_settings(settings)
    mcp_app = module.http_app()

    async def _publish_inbound(message: InboundMessage) -> None:
        """A bridge received a message → normalize it onto ``messaging.inbound`` for the core."""
        await bus.publish(MESSAGING_INBOUND, message.model_dump(), tenant_id=message.tenant)

    async def _on_outbound(event: Event) -> None:
        """The core produced a reply → dispatch it to the bridge it belongs to."""
        try:
            message = OutboundMessage.model_validate(event.json())
        except (ValidationError, ValueError) as exc:
            log.warning("dropping unparseable outbound message", error=str(exc))
            return
        await manager.dispatch(message)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await bus.connect()
            await bus.subscribe(MESSAGING_OUTBOUND, _on_outbound, tenant_id=tenant)
            await manager.start_all(_publish_inbound)
            log.info("messaging service ready", bridges=manager.provider_names(), tenant=tenant)
            try:
                yield
            finally:
                await manager.stop_all()
                await bus.close()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    # GET /manifest — how the core discovers this module (ADR-0004): its events + secrets.
    add_manifest_route(app, module)

    @app.get("/status")
    async def status() -> dict[str, Any]:
        """Live status the shell renders (proxied by the core at /modules/messaging/status):
        the two subjects and a ``bridges`` list, each with connect/enabled/connected state."""
        return await manager.status()

    @app.post("/bridges/{bridge}/reload")
    async def reload_bridge(bridge: str) -> dict[str, Any]:
        """Reconnect one bridge after its token/enabled changed — the core's control path (#369).

        Called by the core right after it writes or clears the bridge's token in OpenBao, so the
        bridge connects/disconnects at runtime without a module restart (ADR-0062). Returns the
        bridge's fresh :class:`~epicurus_messaging.providers.BridgeStatus`. **404** for an unknown
        bridge; **503** if the manager has not started yet.
        """
        try:
            new_status = await manager.reload(bridge)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown bridge {bridge!r}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return new_status.model_dump()

    @app.post("/loopback/inject")
    async def loopback_inject(body: _LoopbackInject) -> dict[str, Any]:
        """Originate an inbound message through the always-on loopback bridge (dev/manual e2e).

        Lets a developer drive the full inbound → turn → outbound path with a model running,
        without a Telegram/Discord account.
        """
        message = await manager.loopback.inject(
            tenant=body.tenant or tenant,
            channel_id=body.channel_id,
            text=body.text,
            thread_id=body.thread_id,
            sender_id=body.sender_id,
            sender_name=body.sender_name,
        )
        return {
            "published": True,
            "session_id": message.session_id(),
            "channel_id": message.channel_id,
        }

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
