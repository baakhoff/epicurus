"""Mail service: ops endpoints + MCP tool surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

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
    _SCOPE_HINT_READ,
    _SCOPE_HINT_SEND,
    MAILBOX_PAGE_ID,
    MODULE_NAME,
    _describe_gmail_error,
    _mark_read_action,
    _mark_unread_action,
    build_mailbox_list,
    build_mailbox_thread,
    build_module,
)
from epicurus_mail.settings import MailSettings


def _service_version() -> str:
    try:
        return pkg_version("epicurus-mail")
    except PackageNotFoundError:
        return "0.0.0"


class MailboxSendRequest(BaseModel):
    """The mailbox page's human-initiated send (ADR-0087) — compose *or* reply.

    With ``reply_to_message_id`` set the module re-derives the recipient/subject/threading
    server-side via ``compose_reply`` (authoritative — the web never handles raw RFC-2822
    headers) and ignores ``to``/``subject``; otherwise it composes a fresh message from
    ``to``/``subject``/``body``/``cc``. Either way it transmits through the same ``/send``
    path the agent-draft confirm uses, but this endpoint is operator-only (never an MCP
    tool -> never the agent, preserving ADR-0085's guarantee).
    """

    body: str
    to: str | None = None
    subject: str | None = None
    cc: str | None = None
    reply_to_message_id: str | None = None


def _content_disposition(filename: str) -> str:
    """A download ``Content-Disposition`` for *filename*, header-safe (no CR/LF/quotes)."""
    safe = filename.replace("\r", "").replace("\n", "").replace('"', "")
    return f'attachment; filename="{safe or "attachment"}"'


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

        A 403 (missing scope or ``usageLimits`` rate limit) or a 429 (throttle) from the provider
        maps to the same reconnect / wait-and-retry hint the tools surface (#513/#538/#557),
        re-raised under Gmail's own status code so the core can relay it to the turn.
        """
        try:
            sent_id = await provider.transmit(message)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_SEND)
            if hint is not None:
                # Preserve Gmail's status (403 scope/usageLimits, or 429 throttle) so the core
                # relays the right code and the hint, not a raw 500 traceback.
                raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
            raise
        # Fulfil the declared ``mail.sent`` contract at the one point a message is actually sent.
        # Tenant-scoped (constraint #1); best-effort — the mail already went out, so a bus hiccup
        # must not fail the send or the resuming turn.
        await _publish_sent(sent_id, message.to, message.subject)
        return {"id": sent_id}

    async def _publish_sent(sent_id: str, to: str, subject: str) -> None:
        """Publish ``mail.sent`` best-effort — a bus hiccup never fails a completed send."""
        try:
            await bus.publish(
                "mail.sent",
                {"id": sent_id, "to": to, "subject": subject},
                tenant_id=settings.default_tenant_id,
            )
        except Exception as exc:  # a bus failure never fails a completed send
            log.warning("mail.sent publish failed", error=str(exc), message_id=sent_id)

    # ── mailbox page (ADR-0087) ──────────────────────────────────────────────
    # The list/thread reads are served here and reached through the core's generic page
    # proxy (query params forwarded, ADR-0023). Send + attachment are gated, mailbox-only
    # core proxies. The module never ships markup — the shell renders the `mailbox` archetype.

    @app.get("/pages/{page_id}")
    async def get_mailbox_page(
        page_id: str,
        label: str | None = None,
        q: str | None = None,
        cursor: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """The `mailbox` archetype data (ADR-0087): the thread list, or one thread.

        ``?thread_id=`` returns the full conversation ``{thread: …}``; otherwise the rail +
        a cursor page of threads for ``?label=``/``?q=``/``?cursor=``. A Gmail 403 (missing
        scope or a ``usageLimits`` rate limit) / 429 is relayed as that status with the
        module's reconnect / wait hint, not a raw 500 (#538/#557).
        """
        if page_id != MAILBOX_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page {page_id!r}")
        try:
            if thread_id:
                return await build_mailbox_thread(provider, thread_id)
            return await build_mailbox_list(provider, label=label, query=q, cursor=cursor)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_READ)
            if hint is not None:
                raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
            raise

    @app.post("/pages/{page_id}/send")
    async def send_mailbox_message(page_id: str, req: MailboxSendRequest) -> dict[str, str]:
        """Human-initiated compose/reply from the mail page (ADR-0087) — shares transmit.

        The operator is the send button: this is reached only through the core's gated,
        operator-only proxy (never an MCP tool -> never the agent, so ADR-0085's structural
        guarantee holds). A reply re-derives its threading here via ``compose_reply``; a
        fresh compose builds a :class:`ComposedMessage` from the fields. Both ``transmit``
        and publish ``mail.sent``.
        """
        if page_id != MAILBOX_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page {page_id!r}")
        try:
            if req.reply_to_message_id:
                message = await provider.compose_reply(req.reply_to_message_id, req.body)
            else:
                recipient = (req.to or "").strip()
                if not recipient:
                    raise HTTPException(status_code=400, detail="a recipient (`to`) is required")
                message = ComposedMessage(
                    to=recipient, subject=req.subject or "", body=req.body, cc=req.cc
                )
            sent_id = await provider.transmit(message)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_SEND)
            if hint is not None:
                raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
            raise
        await _publish_sent(sent_id, message.to, message.subject)
        return {"id": sent_id}

    @app.get("/pages/{page_id}/attachment")
    async def get_mailbox_attachment(
        page_id: str,
        message_id: str = Query(...),
        attachment_id: str = Query(...),
    ) -> Response:
        """Stream one attachment's bytes for the core proxy to relay (ADR-0087).

        The module fetches the bytes from the provider and returns them with the real
        content type + a download disposition; nothing is stored. A missing message /
        attachment is a 404; a Gmail scope/rate-limit error is relayed with its hint.
        """
        if page_id != MAILBOX_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page {page_id!r}")
        try:
            attachment = await provider.get_attachment(message_id, attachment_id)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == httpx.codes.NOT_FOUND:
                raise HTTPException(status_code=404, detail="attachment not found") from exc
            hint = _describe_gmail_error(exc, _SCOPE_HINT_READ)
            if hint is not None:
                raise HTTPException(status_code=code, detail=hint) from exc
            raise
        return Response(
            content=attachment.content,
            media_type=attachment.mime_type,
            headers={"Content-Disposition": _content_disposition(attachment.filename)},
        )

    app.mount("/mcp", mcp_app)

    return app


app = create_app()
