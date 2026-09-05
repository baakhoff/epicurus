"""Mail service: ops endpoints + MCP tool surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core import (
    EntityRef,
    EventBus,
    PlatformClient,
    add_manifest_route,
    add_ops_routes,
    add_portability_routes,
    configure_logging,
    emit_event,
    get_logger,
)
from epicurus_mail.cache import CachedMailbox
from epicurus_mail.db import MailCache
from epicurus_mail.gmail import GmailProvider
from epicurus_mail.poller import run_periodic
from epicurus_mail.portability import MailPortability
from epicurus_mail.provider import ComposedMessage, MailNotConnected
from epicurus_mail.service import (
    _NOT_CONNECTED_HINT,
    _SCOPE_HINT,
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

# What a route raises when no Google account is connected (#764). 503 rather than the token
# endpoint's own 404/400: those describe the *core's* view of a missing token, and a 404 out
# of a page/message route already means "no such page / no such message" — reusing it would
# make "you haven't connected Google" indistinguishable from "that id is wrong". 503 says the
# dependency this module is built on isn't available right now, which is exactly true and is
# what the shell relays verbatim. The list read never reaches here: it answers with the honest
# empty payload instead, so plain navigation to Mail is never an error.
_NOT_CONNECTED_STATUS = 503


def _not_connected() -> HTTPException:
    """The uniform "Google is not connected" HTTP failure (#764) — one wording, one status."""
    return HTTPException(status_code=_NOT_CONNECTED_STATUS, detail=_NOT_CONNECTED_HINT)


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


class MarkThreadReadRequest(BaseModel):
    """The mailbox page's mark-read-on-open request (#625).

    Opening a conversation marks its unread messages read — ``message_ids`` are the ones to
    clear (the shell passes only those already unread). ``thread_id`` lets the module write the
    read state through to the local cache (ADR-0096) so the list row converges at once, not only
    on the next reconcile. Marking a whole thread read is the one case where a thread-level cache
    write-through is unambiguous (every message is being cleared).
    """

    thread_id: str
    message_ids: list[str]


def _content_disposition(filename: str) -> str:
    """A download ``Content-Disposition`` for *filename*, header-safe (no CR/LF/quotes)."""
    safe = filename.replace("\r", "").replace("\n", "").replace('"', "")
    return f'attachment; filename="{safe or "attachment"}"'


def create_app(*, engine: AsyncEngine | None = None) -> FastAPI:
    """Build the mail ASGI app.

    Args:
        engine: An optional pre-built async engine for the local cache (ADR-0096). Tests inject
            an in-memory SQLite engine; production defaults to a Postgres engine on
            ``settings.database_url``.
    """
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

    # Tenant-scoped local cache (ADR-0096, #623): the landing view serves from here instantly
    # and a background reconcile pulls only the delta, so opening Mail no longer fans out ~28
    # Gmail calls on every open. The module owns its own tables; state is externalized to
    # Postgres (constraint #2).
    engine = engine or create_async_engine(settings.database_url)
    cache = MailCache(engine)
    mailbox = CachedMailbox(
        provider,
        cache,
        tenant_id=settings.default_tenant_id,
        bus=bus,
        provider_name="gmail",
        sync_failed_cooldown_s=settings.mail_sync_failed_cooldown_s,
        category_ttl_s=settings.mail_category_ttl_s,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with module.mcp.session_manager.run():
            await cache.init()
            await bus.connect()
            # The background reconcile (#796) — mail's first periodic job, the same pattern
            # calendar and tasks already use. Without it `mail.received` only ever fired while
            # someone had the Mail page open, so no automation or push alert on new mail could
            # run unattended. Started/cancelled around the app lifetime; `MAIL_POLL_INTERVAL_S=0`
            # returns immediately, leaving a task that is already done.
            poller_task = asyncio.create_task(
                run_periodic(
                    mailbox=mailbox,
                    provider=provider,
                    tenant=settings.default_tenant_id,
                    poll_interval_s=settings.mail_poll_interval_s,
                )
            )
            log.info("mail service ready", tenant=settings.default_tenant_id)
            try:
                yield
            finally:
                poller_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poller_task
                await bus.close()
                await engine.dispose()

    app = FastAPI(title=MODULE_NAME, lifespan=lifespan)
    add_ops_routes(app, service_name=MODULE_NAME, version=_service_version())
    add_manifest_route(app, module)
    # Tenant data portability (#867/#874): every mail table is a Gmail-derived cache (see
    # epicurus_mail.portability), so this always exports zero records — the routes exist so
    # mail appears in the archive with an honest count rather than a silent absence.
    add_portability_routes(app, module, MailPortability())

    @app.get("/status")
    async def get_status() -> dict[str, Any]:
        """Gmail connection status for the manifest-driven UI status panel.

        Reports whether a Google token is available — a fast credential check via the core
        (``availability``), not a live Gmail API call. The old live ``/users/me/profile``
        probe could exceed the core's status-proxy timeout and surface as a Bad Gateway when
        the panel polled it (#209).

        Three fields, because there are three answers (#835): ``connection`` is the state
        itself (``connected`` / ``not_connected`` / ``unreachable``) and ``detail`` its
        one-clause reason (``null`` when connected). ``gmail_connected`` is kept, with exactly
        its old meaning (true only for ``connected``), so nothing reading the boolean has to
        change — but it is the field that cannot tell an operator whose core is down from one
        who never connected Google, which is why it is no longer the whole payload. The panel
        renders whatever keys it is given (ADR-0018), so this needs no shell change.
        """
        availability = await provider.availability()
        return {
            "gmail_connected": availability.connected,
            "connection": availability.state,
            "detail": availability.reason,
        }

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
        except MailNotConnected as exc:
            # Ahead of the blanket 404 below: a chip left over from a connected session must
            # not claim the message was deleted when the truth is that Google is gone (#764).
            raise _not_connected() from exc
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
        except MailNotConnected as exc:
            raise _not_connected() from exc
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
            # HTML body + attachments (ADR-0097, #627) so the panel reader renders rich mail
            # too, resolving inline ``cid:`` images through the module's attachment proxy.
            "body_html": message.body_html,
            "attachments": [att.model_dump() for att in message.attachments],
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
        except MailNotConnected as exc:
            # Google was disconnected between composing the draft and confirming it (#764) —
            # the operator gets the reconnect sentence, not a traceback in the split-pane.
            raise _not_connected() from exc
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_SEND)
            if hint is not None:
                # Preserve Gmail's status (403 scope/usageLimits, or 429 throttle) so the core
                # relays the right code and the hint, not a raw 500 traceback.
                raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
            raise
        # Fulfil the declared ``mail.sent`` contract at the one point a message is actually sent.
        # Tenant-scoped (constraint #1); best-effort — the mail already went out, so a spine
        # hiccup must not fail the send or the resuming turn.
        await _publish_sent(sent_id, message.to, message.subject)
        return {"id": sent_id}

    async def _publish_sent(sent_id: str, to: str, subject: str) -> None:
        """Emit ``mail.sent`` (#663) on the module event spine — a spine hiccup never fails an
        already-completed send."""
        clean_subject = (subject or "(no subject)")[:200]
        try:
            await emit_event(
                bus,
                tenant_id=settings.default_tenant_id,
                module="mail",
                event_type="mail.sent",
                dedup_key=sent_id,
                payload={"to": to[:200], "subject": clean_subject},
                entity_ref=EntityRef(
                    ref_id=sent_id, module="mail", kind="message", title=clean_subject, summary=to
                ),
            )
        except Exception as exc:  # a spine hiccup never fails a completed send
            log.warning("mail.sent emit failed", error=str(exc), message_id=sent_id)

    # ── mailbox page (ADR-0087) ──────────────────────────────────────────────
    # The list/thread reads are served here and reached through the core's generic page
    # proxy (query params forwarded, ADR-0023). Send + attachment are gated, mailbox-only
    # core proxies. The module never ships markup — the shell renders the `mailbox` archetype.

    @app.get("/pages/{page_id}")
    async def get_mailbox_page(
        page_id: str,
        label: str | None = None,
        q: str | None = None,
        tab: str | None = None,
        cursor: str | None = None,
        thread_id: str | None = None,
        reconcile: bool = False,
    ) -> dict[str, Any]:
        """The `mailbox` archetype data (ADR-0087): the thread list, or one thread.

        ``?thread_id=`` returns the full conversation ``{thread: …}``; otherwise the rail +
        a cursor page of threads for ``?label=``/``?q=``/``?cursor=``. On the Inbox the payload
        also carries the category ``tabs`` (#765), and ``?tab=<id>`` scopes the list to one of
        them. The plain landing view serves from the local cache instantly (ADR-0096, #623);
        ``?reconcile=1`` first pulls the provider delta into the cache (the web fires it as a
        background second read, so the list updates in place without a manual refresh). Search,
        a tab-scoped list, and deeper pages read live. A Gmail 403 (missing scope or a
        ``usageLimits`` rate limit) / 429 is relayed as that status with the module's reconnect
        / wait hint, not a raw 500 (#538/#557).

        With **no Google account connected** (#764) the list read returns an empty payload
        carrying ``disconnected: true`` — the shell renders its honest empty state, so opening
        Mail on a self-host that never connected Google is not an error. A ``?thread_id=``
        read has nothing honest to return and fails with 503 + the reconnect sentence.

        When the module could not *find out* whether an account is connected (#835), the list
        read returns that same empty payload carrying ``unreachable: "<reason>"`` instead, and
        **never** ``disconnected``: the shell keys its "connect Google in Settings" panel off
        that flag, and sending an operator to reconnect a working account is the expensive
        mistake this distinction exists to prevent.
        """
        if page_id != MAILBOX_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page {page_id!r}")
        try:
            if thread_id:
                return await build_mailbox_thread(provider, thread_id)
            return await build_mailbox_list(
                provider,
                mailbox=mailbox,
                label=label,
                query=q,
                tab=tab,
                cursor=cursor,
                reconcile=reconcile,
            )
        except MailNotConnected as exc:
            # Only a `?thread_id=` read can land here: the list read answers with the honest
            # empty payload (#764) rather than raising, so plain navigation to Mail never
            # errors. Opening a specific conversation with no account is a real dead end.
            raise _not_connected() from exc
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
        except MailNotConnected as exc:
            raise _not_connected() from exc
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_SEND)
            if hint is not None:
                raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
            raise
        await _publish_sent(sent_id, message.to, message.subject)
        return {"id": sent_id}

    @app.post("/pages/{page_id}/mark-read")
    async def mark_thread_read(page_id: str, req: MarkThreadReadRequest) -> dict[str, Any]:
        """Mark a thread's messages read on open (#625) — provider + local-cache write-through.

        Wires the reader's *open* event to the existing mark-read seam (`provider.set_unread`,
        #277): clears the unread flag on each of ``message_ids`` at the provider, then writes the
        thread's read state through to the local cache (ADR-0096) so the list row is already read
        without waiting for the next reconcile. The shell flips the row optimistically and calls
        this in the background, reverting (via a refetch) only if it fails. A Gmail scope /
        rate-limit error is relayed with its hint, as with the mark tools. Operator-only via the
        gated core proxy (never an MCP tool). An empty ``message_ids`` is a no-op.
        """
        if page_id != MAILBOX_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page {page_id!r}")
        try:
            for message_id in req.message_ids:
                await provider.set_unread(message_id, unread=False)
        except MailNotConnected as exc:
            raise _not_connected() from exc
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT)
            if hint is not None:
                raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
            raise
        # Only after every provider mark succeeds: converge the cache so the row reads correctly
        # even against a reconcile that races the open (a partial failure above leaves the cache
        # untouched and lets the next reconcile settle it).
        if req.message_ids:
            await mailbox.mark_thread_read(req.thread_id, unread=False)
        return {"thread_id": req.thread_id, "marked": len(req.message_ids)}

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
        except MailNotConnected as exc:
            raise _not_connected() from exc
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
