"""Runnable messaging service: ops endpoints + manifest, with the bridge wired to NATS.

It connects the active :class:`~epicurus_messaging.providers.BridgeProvider` to the inbox
contract: a message the bridge receives is published on ``messaging.inbound`` (the core runs
the turn), and each ``messaging.outbound`` reply the core emits is handed to the bridge to
deliver. The module never calls an LLM (constraint #8).

The outbound subscription is under the configured default tenant (single-tenant v1, mirroring
the core's inbound consumer); multi-tenant fan-out is the same follow-up noted in ADR-0058.
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
from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.service import MODULE_NAME, build_module, build_provider
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
    provider = build_provider(settings, secrets)
    module = build_module(provider)
    bus = EventBus.from_settings(settings)
    mcp_app = module.http_app()

    async def _publish_inbound(message: InboundMessage) -> None:
        """A bridge received a message → normalize it onto ``messaging.inbound`` for the core."""
        await bus.publish(MESSAGING_INBOUND, message.model_dump(), tenant_id=message.tenant)

    async def _on_outbound(event: Event) -> None:
        """The core produced a reply → deliver it via the active bridge."""
        try:
            message = OutboundMessage.model_validate(event.json())
        except (ValidationError, ValueError) as exc:
            log.warning("dropping unparseable outbound message", error=str(exc))
            return
        await provider.send(message)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await bus.connect()
            await bus.subscribe(MESSAGING_OUTBOUND, _on_outbound, tenant_id=tenant)
            await provider.start(_publish_inbound)
            log.info("messaging service ready", provider=provider.provider_name(), tenant=tenant)
            try:
                yield
            finally:
                await provider.stop()
                await bus.close()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    # GET /manifest — how the core discovers this module (ADR-0004): its events + secrets.
    add_manifest_route(app, module)

    @app.get("/status")
    async def status() -> dict[str, Any]:
        """Live status the shell renders (proxied by the core at /modules/messaging/status)."""
        body: dict[str, Any] = {
            "provider": provider.provider_name(),
            # The loopback bridge is always in-process; real bridges reflect their token/connection.
            "connected": isinstance(provider, LoopbackProvider),
            "inbound_subject": MESSAGING_INBOUND,
            "outbound_subject": MESSAGING_OUTBOUND,
        }
        if isinstance(provider, LoopbackProvider):
            body["delivered"] = len(provider.sent)
        return body

    @app.post("/loopback/inject")
    async def loopback_inject(body: _LoopbackInject) -> dict[str, Any]:
        """Originate an inbound message through the loopback bridge (dev/manual e2e).

        Only valid for the loopback provider; a real bridge originates from its own
        webhook/poller. Lets a developer drive the full inbound → turn → outbound path with a
        model running, without a Telegram/Discord account.
        """
        if not isinstance(provider, LoopbackProvider):
            raise HTTPException(
                status_code=404,
                detail="loopback inject is only available for the loopback provider",
            )
        message = await provider.inject(
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
