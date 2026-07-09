"""Mail service: ops endpoints + MCP tool surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from epicurus_core import (
    EventBus,
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    configure_logging,
    get_logger,
)
from epicurus_mail.gmail import GmailProvider
from epicurus_mail.provider import ComposedMessage
from epicurus_mail.service import (
    _SCOPE_HINT_SEND,
    MODULE_NAME,
    _describe_403,
    build_module,
)
from epicurus_mail.settings import MailSettings


def _service_version() -> str:
    try:
        return pkg_version("epicurus-mail")
    except PackageNotFoundError:
        return "0.0.0"


def _mark_read_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` (ADR-0024) the reader renders to mark an unread message read."""
    return {
        "tool": "mail_mark_read",
        "label": "Mark as read",
        "intent": "default",
        "icon": "check",
        "args": {"message_id": message_id},
    }


def _mark_unread_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` (ADR-0024) the reader renders to mark a read message unread."""
    return {
        "tool": "mail_mark_unread",
        "label": "Mark as unread",
        "intent": "default",
        "icon": "mail",
        "args": {"message_id": message_id},
    }


def create_app() -> FastAPI:
    """Build the mail ASGI app."""
    settings = MailSettings(service_name=MODULE_NAME)
    configure_logging(settings)
    log = get_logger(MODULE_NAME)

    platform = PlatformClient(
        base_url=settings.platform_url,
        tenant_id=settings.default_tenant_id,
    )
    provider = GmailProvider(platform=platform, tenant_id=settings.default_tenant_id)
    bus = EventBus.from_settings(settings)
    module = build_module(provider)
    mcp_app = module.http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await bus.connect()
            log.info("mail service ready", tenant=settings.default_tenant_id)
            try:
                yield
            finally:
                await bus.close()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Gmail connection status for the manifest-driven UI status panel.

        Reports whether a Google token is available — a fast credential check via the core
        (``is_available``), not a live Gmail API call. The old live ``/users/me/profile``
        probe could exceed the core's status-proxy timeout and surface as a Bad Gateway when
        the panel polled it (#209).
        """
        return {"gmail_connected": await provider.is_available()}

    @app.get("/resolve/message/{ref_id}")
    async def resolve_message(ref_id: str) -> dict[str, Any]:
        """Hover-card resolver for a mail message entity (ADR-0019).

        Returns a compact HoverCard envelope — subject as title, snippet as
        description, and detail rows for unread status (only when unread), sender,
        recipients, and date — for display in the inline hover-card. No ``href``:
        the chip's click opens the read-only ``email-reader`` panel directly (the
        full message is served by ``GET /messages/{ref_id}``), so there is no
        outbound URL to carry.
        """
        try:
            message = await provider.read(ref_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"message {ref_id!r} not found") from exc
        details: list[dict[str, str]] = []
        # An unread flag is the actionable signal, so lead with it; read messages omit
        # the row entirely rather than render a redundant "Read" on every message.
        if message.unread:
            details.append({"label": "Status", "value": "Unread"})
        details.append({"label": "From", "value": message.sender})
        if message.to:
            details.append({"label": "To", "value": ", ".join(message.to)})
        if message.date:
            details.append({"label": "Date", "value": message.date})
        return {
            "title": message.subject or "(no subject)",
            "description": message.snippet,
            "details": details,
        }

    @app.get("/messages/{ref_id}")
    async def get_message(ref_id: str) -> dict[str, Any]:
        """Full email message for the panel's email-reader view (ADR-0019).

        Returns an EmailMessage envelope — subject, from, date, the decoded plain-text
        body, the message's ``unread`` state, and a single tool-backed ``actions`` entry
        (ADR-0024): the reader renders it as a **Mark as read** (when unread) or **Mark as
        unread** (when read) toggle that invokes the matching MCP tool through the core
        proxy. ``module``/``message_id`` let the reader re-fetch itself after the toggle.
        """
        try:
            message = await provider.read(ref_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"message {ref_id!r} not found") from exc
        toggle = (
            _mark_read_action(message.id) if message.unread else _mark_unread_action(message.id)
        )
        return {
            "subject": message.subject or "(no subject)",
            "from": message.sender,
            "date": message.date,
            "body": message.body or "",
            "module": MODULE_NAME,
            "message_id": message.id,
            "unread": message.unread,
            "actions": [toggle],
        }

    @app.post("/send")
    async def send_message(message: ComposedMessage) -> dict[str, str]:
        """Transmit an operator-confirmed draft — the mail module's **only** send path (ADR-0085).

        The core POSTs here after the operator Confirms a draft in the split-pane (#563); it is a
        plain HTTP endpoint, **not** an MCP tool, so the agent can never reach it — the draft-first
        guarantee is that the model can compose but only a human confirm sends. ``message`` is the
        exact :class:`ComposedMessage` that was shown, so the bytes sent are byte-identical to the
        reviewed draft. Publishes ``mail.sent`` (best-effort) and returns the provider message id.

        A 403 from the provider maps to the same reconnect / rate-limit hint the tools surface
        (#513/#538), raised as an HTTP 403 so the core can relay it to the turn.
        """
        try:
            sent_id = await provider.transmit(message)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                raise HTTPException(
                    status_code=403, detail=_describe_403(exc, _SCOPE_HINT_SEND)
                ) from exc
            raise
        # Fulfil the declared ``mail.sent`` contract at the one point a message is actually sent.
        # Tenant-scoped (constraint #1); best-effort — the mail already went out, so a bus hiccup
        # must not fail the send or the resuming turn.
        try:
            await bus.publish(
                "mail.sent",
                {"id": sent_id, "to": message.to, "subject": message.subject},
                tenant_id=settings.default_tenant_id,
            )
        except Exception as exc:  # a bus failure never fails a completed send
            log.warning("mail.sent publish failed", error=str(exc), message_id=sent_id)
        return {"id": sent_id}

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
