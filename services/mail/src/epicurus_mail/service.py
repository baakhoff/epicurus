"""Mail module — MCP tool surface (ADR-0016).

Provider-agnostic tools: ``mail_search``, ``mail_read``, ``mail_send``, ``mail_reply``,
and the read-state pair ``mail_mark_read`` / ``mail_mark_unread``.
The tool names and signatures are domain-neutral; no Gmail specifics appear in the
tool surface (the manifest declares Gmail's OAuth scopes for the connect flow, #241).
``mail_search`` returns a :func:`~epicurus_core.tool_envelope` so the UI renders
each result as an entity-reference chip (ADR-0019): hover for the hover-card,
click to open the full message in the right-panel email-reader.
``mail_send`` and ``mail_reply`` are **draft-first** (ADR-0085, #563): they *compose* a
message and return a :func:`~epicurus_core.draft_review` envelope — they never transmit.
The core suspends the turn and shows the draft in a split-pane; only the operator's Confirm
triggers the actual send (the module's ``POST /send`` endpoint, invoked by the core). The MCP
surface therefore exposes **no** tool that sends, so the agent cannot deliver mail on its own.
``mail_reply`` (#461) composes a reply that stays in the original conversation thread —
RFC-2822 ``In-Reply-To``/``References`` plus the provider's native thread association —
deriving the recipient and subject from the original message rather than taking them as
arguments.
``mail_mark_read`` / ``mail_mark_unread`` flip a message's read state; the
``email-reader`` panel also surfaces them as a tool-backed toggle (ADR-0024).
"""

from __future__ import annotations

from typing import Any

import httpx

from epicurus_core import (
    EntityRef,
    EpicurusModule,
    PageSpec,
    UiAction,
    UiSection,
    capped_listing,
    draft_review,
    tool_envelope,
)
from epicurus_mail.cache import CachedMailbox
from epicurus_mail.gmail import GMAIL_API_SCOPES
from epicurus_mail.provider import ComposedMessage, MailMessage, MailProvider

MODULE_NAME = "mail"
MAILBOX_PAGE_ID = "mailbox"
# The default folder the page opens on, and the Gmail label the nav-badge unread reflects.
DEFAULT_LABEL = "INBOX"
# The kind every mail entity-reference / attachment carries (ADR-0019).
MESSAGE_KIND = "message"
# Cap on threads per list page (ADR-0087). Each thread costs a metadata fetch, so a bounded
# page keeps one request from fanning out across an unbounded mailbox (#539); the shell pages
# further with the returned cursor.
MAILBOX_PAGE_SIZE = 25

# Shown when ``messages.modify`` is rejected for lack of scope — the operator connected
# Google before mail required ``gmail.modify`` and must reconnect to grant it.
_SCOPE_HINT = (
    "Couldn't change the read state: the connected Google account is missing the Gmail"
    " modify permission. Reconnect Google (Settings → Connect) to grant it."
)

# Shown when ``messages.send`` is rejected for lack of scope (#513) — mirrors _SCOPE_HINT's
# reconnect-hint treatment for send/reply instead of a bare exception.
_SCOPE_HINT_SEND = (
    "Couldn't send: the connected Google account is missing the Gmail send permission."
    " Reconnect Google (Settings → Connect) to grant it."
)

# Shown when archive/trash (``messages.modify`` / ``messages.trash``) is rejected for lack of
# scope (ADR-0087). Both need ``gmail.modify`` (already granted for mark read/unread), so a
# bare 403 there means the operator connected before mail required it and must reconnect.
_SCOPE_HINT_TRIAGE = (
    "Couldn't move the message: the connected Google account is missing the Gmail modify"
    " permission. Reconnect Google (Settings → Connect) to grant it."
)

# Shown when mail_reply's own metadata lookup (the original message's Reply-To/From/
# Subject/Message-ID — needs gmail.modify) is rejected for lack of scope. Distinct wording
# from _SCOPE_HINT: that constant talks about "the read state", which doesn't apply here —
# the reply was never composed, let alone sent (#538).
_SCOPE_HINT_REPLY_LOOKUP = (
    "Couldn't reply: the connected Google account is missing the Gmail modify permission"
    " needed to look up the original message. Reconnect Google (Settings → Connect) to"
    " grant it."
)

# Shown when a read (``mail_search`` / ``mail_read``) is rejected for lack of scope — mail
# needs ``gmail.modify`` (which covers reads); a bare 403 there means the operator hasn't
# granted it. Read-context wording, distinct from the modify/send/reply hints above.
_SCOPE_HINT_READ = (
    "Couldn't reach Gmail: the connected Google account is missing the Gmail permission this"
    " needs. Reconnect Google (Settings → Connect) to grant it."
)

# Gmail returns 403 both for a missing OAuth scope and for per-user/per-day rate limiting
# (``usageLimits``) — the reasons below are Google's **legacy** Discovery-API error codes for
# the latter. Blaming every 403 on a missing scope misdirects an operator who is simply being
# throttled (#538). Google's newer APIs report the same thing in the AIP-193 shape instead
# (``error.status == "RESOURCE_EXHAUSTED"`` / ``error.details[].reason == "RATE_LIMIT_EXCEEDED"``);
# :func:`_is_rate_limited` checks both so a shape migration doesn't silently misfire (#557).
_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}
)
_AIP_RATE_LIMIT_STATUS = "RESOURCE_EXHAUSTED"
_AIP_RATE_LIMIT_REASON = "RATE_LIMIT_EXCEEDED"

_RATE_LIMIT_HINT = (
    "Gmail is rate-limiting this account (too many requests in a short time, or the daily"
    " quota was reached). Wait a bit and try again."
)


def _is_rate_limited(error: dict[str, Any]) -> bool:
    """Whether a Gmail error body names a rate-limit cause, in either shape (#538, #557).

    Legacy (Gmail v1 today): ``error.errors[].reason`` ∈ :data:`_RATE_LIMIT_REASONS`. Modern
    AIP-193 (if Gmail ever migrates): ``error.status == "RESOURCE_EXHAUSTED"`` or an
    ``error.details[]`` entry whose ``reason`` is ``"RATE_LIMIT_EXCEEDED"``. Every field access
    is defensive — a non-string ``reason`` (an otherwise well-formed body with a nested object
    there) is skipped, not fed to the ``in`` membership test where it would raise ``TypeError``
    on an unhashable value instead of falling back to the scope hint.
    """
    if error.get("status") == _AIP_RATE_LIMIT_STATUS:
        return True
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason") == _AIP_RATE_LIMIT_REASON:
                return True
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                reason = item.get("reason")
                if isinstance(reason, str) and reason in _RATE_LIMIT_REASONS:
                    return True
    return False


def _rate_limit_hint(response: httpx.Response) -> str:
    """The rate-limit hint, extended with Gmail's ``Retry-After`` when it sends one (#557).

    ``Retry-After`` is either a number of seconds or an HTTP-date; surface whichever it is so
    the operator knows how long to wait rather than guessing.
    """
    retry_after = (response.headers.get("Retry-After") or "").strip()
    if not retry_after:
        return _RATE_LIMIT_HINT
    if retry_after.isdigit():
        return f"{_RATE_LIMIT_HINT} Gmail suggests waiting about {retry_after} seconds."
    return f"{_RATE_LIMIT_HINT} Gmail asks you to retry after {retry_after}."


def _describe_403(exc: httpx.HTTPStatusError, scope_hint: str) -> str:
    """The user-facing hint for a Gmail 403: *scope_hint* unless the error body names a
    rate-limit cause (#538) — recognized in either the legacy or the AIP-193 shape (#557) — in
    which case that's the real cause. Falls back to *scope_hint* whenever the body doesn't parse
    into Google's error shape, since a missing scope is the far more common cause of an
    unparseable 403.
    """
    try:
        error = exc.response.json()["error"]
    except (ValueError, KeyError, TypeError):
        return scope_hint
    if isinstance(error, dict) and _is_rate_limited(error):
        return _rate_limit_hint(exc.response)
    return scope_hint


def _describe_gmail_error(exc: httpx.HTTPStatusError, scope_hint: str) -> str | None:
    """A hint a tool can return in place of a raw exception for a Gmail HTTP error, or ``None``
    to signal "not one we soften — re-raise" (#538, #557).

    A **429** (Too Many Requests) is unambiguously rate limiting → the wait-and-retry hint
    (honoring ``Retry-After``). A **403** may be a missing scope *or* a rate limit disguised as
    ``usageLimits`` → :func:`_describe_403` disambiguates. Any other status returns ``None``.
    """
    status = exc.response.status_code
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return _rate_limit_hint(exc.response)
    if status == httpx.codes.FORBIDDEN:
        return _describe_403(exc, scope_hint)
    return None


def _draft_summary(message: ComposedMessage) -> str:
    """A one-line label for a composed draft — shown in the turn activity and logs (ADR-0085)."""
    return f"Email to {message.to} — {message.subject or '(no subject)'}"


def build_module(provider: MailProvider) -> EpicurusModule:
    """Build the mail module and register its MCP tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.12.0",
        description=(
            "Provider-agnostic mail — search, read, and draft-first send/reply. Gmail is the v0.1"
            " provider."
        ),
        ui=UiSection(
            icon="mail",
            summary=(
                "Lets the agent search, read, and compose mail via your connected Google account."
                " The agent never sends on its own — it composes a draft you Confirm or Decline"
                " (ADR-0085). Requires Gmail scopes on the Google OAuth connection."
            ),
            status_url="/status",
            # Draft-first (ADR-0085): these compose a draft the assistant shows for your review;
            # they are no longer one-tap sends, so they are not danger actions (nothing is
            # delivered until you Confirm the draft). The real send is the ``POST /send`` endpoint.
            actions=[
                UiAction(
                    tool="mail_send",
                    label="Compose mail",
                    description=(
                        "Compose a message. The assistant shows it for your review — nothing is"
                        " sent until you confirm the draft."
                    ),
                ),
                UiAction(
                    tool="mail_reply",
                    label="Compose reply",
                    description=(
                        "Compose a reply in the same conversation thread. Shown for your review;"
                        " nothing is sent until you confirm the draft."
                    ),
                ),
            ],
        ),
        # A left-nav Mail page (ADR-0087): the module supplies labels/threads/messages and
        # the core shell renders the `mailbox` client (rail -> thread list -> conversation +
        # compose/reply). No module markup. Reads flow through the generic page proxy; the
        # send + attachment endpoints are gated, mailbox-only core proxies.
        pages=[
            PageSpec(
                id=MAILBOX_PAGE_ID,
                title="Mail",
                archetype="mailbox",
                icon="mail",
                nav_order=50,
            )
        ],
        resolver=True,
        # The Gmail API scopes the shell requests when connecting Google (#241); the core
        # adds the default identity scopes. Without these, Gmail API calls return 403.
        oauth_scopes={"google": GMAIL_API_SCOPES},
    )

    module.emits("mail.sent", "Published after a message is sent successfully.")

    @module.tool()
    async def mail_search(query: str, max_results: int = 10) -> str:
        """Search for mail matching *query*.

        Supports the same query syntax as Gmail (e.g. ``from:alice``,
        ``subject:invoice``, ``is:unread``).  Returns up to *max_results*
        messages as entity-reference chips — hover for a quick preview, click
        to open the full message in the panel.  Each chip carries the message
        id; call ``mail_read`` explicitly only when you need the body as text.

        Args:
            query: Mail search expression (Gmail query syntax).
            max_results: Maximum number of messages to return (1-50, default 10).
        """
        capped = max(1, min(max_results, 50))
        try:
            messages = await provider.search(query, capped)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_READ)
            if hint is not None:
                return hint  # a rate-limit (429/403) or missing-scope hint, not a raw traceback
            raise
        if not messages:
            return tool_envelope("No messages found.", [])
        refs = [
            EntityRef(
                ref_id=m.id,
                module=MODULE_NAME,
                kind="message",
                title=m.subject or "(no subject)",
                summary=m.snippet,
            )
            for m in messages
        ]
        lines = [
            f"- [{m.subject or '(no subject)'}] from {m.sender}"
            + (f" ({m.date})" if m.date else "")
            for m in messages
        ]
        # Capped the same way as the entity-ref id block the core appends (both default to
        # LIST_CAP, #468/#522) — max_results is already clamped to 50 above so this can't
        # bite today, but it keeps mail_search consistent with calendar's adoption (#539)
        # rather than reinventing the listing text.
        text = capped_listing(lines, noun="message")
        return tool_envelope(text, refs)

    @module.tool()
    async def mail_read(message_id: str) -> str:
        """Fetch the full content of a mail message by its *message_id*.

        Returns the message subject, sender, date, and decoded plain-text body
        as a readable block.  Use ``mail_search`` first to discover message IDs.
        The UI opens the full message in the right-panel when a user clicks an
        email chip — call this tool only when you need the body as text for
        reasoning or quoting.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            m = await provider.read(message_id)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_READ)
            if hint is not None:
                return hint
            raise
        parts = [f"Subject: {m.subject or '(no subject)'}"]
        parts.append(f"From: {m.sender}")
        if m.date:
            parts.append(f"Date: {m.date}")
        parts.append("")
        parts.append(m.body or "(no body)")
        return "\n".join(parts)

    @module.tool()
    async def mail_send(to: str, subject: str, body: str) -> str:
        """Compose an email for the user to review — this does **not** send it (ADR-0085).

        There is no tool that sends. You compose the message; the user reviews it in a split-pane
        and presses **Confirm** to send or **Decline** to drop it. Compose freely — the user is
        the send button. On Decline you are told (with any reason they give) so you can revise and
        compose again.

        Args:
            to: Recipient email address.
            subject: Message subject line.
            body: Plain-text message body.

        Pauses the turn to show the draft; the turn resumes once the user confirms or declines.
        """
        recipient = to.strip()
        if not recipient:
            return "error: a recipient (`to`) is required to compose a message."
        message = ComposedMessage(to=recipient, subject=subject, body=body)
        return draft_review(
            kind="mail",
            module=MODULE_NAME,
            summary=_draft_summary(message),
            draft=message.model_dump(),
        )

    @module.tool()
    async def mail_reply(message_id: str, body: str) -> str:
        """Compose a reply for the user to review — this does **not** send it (ADR-0085).

        Like ``mail_send`` this composes only. The reply stays in the original conversation
        thread; the recipient and subject are derived from the original message (preferring its
        ``Reply-To`` over its sender when it carries one, and its subject prefixed with "Re:"
        unless already a reply), so you supply just the body. The user reviews the draft in a
        split-pane and **Confirm**s or **Decline**s — no tool sends; the user is the send button.
        The reply body is sent **clean**: it is not auto-quoted with the original message's text.

        Args:
            message_id: The message being replied to (from ``mail_search`` or ``mail_read``).
            body: Plain-text reply body.

        Pauses the turn to show the draft; the turn resumes once the user confirms or declines.
        """
        try:
            message = await provider.compose_reply(message_id, body)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_REPLY_LOOKUP)
            if hint is not None:
                return hint
            raise
        return draft_review(
            kind="mail",
            module=MODULE_NAME,
            summary=_draft_summary(message),
            draft=message.model_dump(),
        )

    @module.tool()
    async def mail_mark_read(message_id: str) -> str:
        """Mark a mail message as read.

        Clears the unread flag on the message identified by *message_id* (discover ids
        with ``mail_search``). Distinct from ``mail_read``, which fetches the body —
        this only changes read state and returns nothing to read. Idempotent.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.set_unread(message_id, unread=False)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT)
            if hint is not None:
                return hint
            raise
        return f"marked-read:{message_id}"

    @module.tool()
    async def mail_mark_unread(message_id: str) -> str:
        """Mark a mail message as unread.

        Restores the unread flag on the message identified by *message_id* (discover ids
        with ``mail_search``). Idempotent.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.set_unread(message_id, unread=True)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT)
            if hint is not None:
                return hint
            raise
        return f"marked-unread:{message_id}"

    @module.tool()
    async def mail_archive(message_id: str) -> str:
        """Archive a mail message — remove it from the Inbox without deleting it.

        The message stays in All Mail and is fully recoverable; this only takes it out of
        the Inbox (discover ids with ``mail_search``). Idempotent — archiving an
        already-archived message is a no-op.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.archive(message_id)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_TRIAGE)
            if hint is not None:
                return hint
            raise
        return f"archived:{message_id}"

    @module.tool()
    async def mail_trash(message_id: str) -> str:
        """Move a mail message to Trash — recoverable, not a permanent delete.

        The message goes to Trash (auto-purged by the provider after its retention window)
        and can be restored until then; this is **not** a permanent delete. Discover ids
        with ``mail_search``. Idempotent.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.trash(message_id)
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_TRIAGE)
            if hint is not None:
                return hint
            raise
        return f"trashed:{message_id}"

    return module


# ── mailbox page data (ADR-0087) ──────────────────────────────────────────────
# Pure builders the app's page routes call; unit-testable against a mocked provider.
# Every mutation is a `BoardAction` (ADR-0024) naming an existing MCP tool, so the page
# mutates through the same validated tool proxy the agent uses — no module markup.


def _mark_read_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` (ADR-0024) that marks an unread message read."""
    return {
        "tool": "mail_mark_read",
        "label": "Mark as read",
        "intent": "default",
        "icon": "check",
        "args": {"message_id": message_id},
    }


def _mark_unread_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` (ADR-0024) that marks a read message unread."""
    return {
        "tool": "mail_mark_unread",
        "label": "Mark as unread",
        "intent": "default",
        "icon": "mail",
        "args": {"message_id": message_id},
    }


def _archive_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` that archives a message out of the Inbox (ADR-0087)."""
    return {
        "tool": "mail_archive",
        "label": "Archive",
        "intent": "default",
        "icon": "archive",
        "args": {"message_id": message_id},
    }


def _trash_action(message_id: str) -> dict[str, Any]:
    """A danger `BoardAction` that moves a message to Trash (ADR-0087).

    A danger action must carry a confirm prompt (the shared BoardAction contract), so the
    shell gates it behind a dialog — trash is recoverable but still a triage step the
    operator should mean.
    """
    return {
        "tool": "mail_trash",
        "label": "Trash",
        "intent": "danger",
        "icon": "trash",
        "args": {"message_id": message_id},
        "confirm": "Move this message to Trash?",
    }


def message_payload(message: MailMessage) -> dict[str, Any]:
    """One thread-pane message as the shared `email-reader` shape + attachments (ADR-0087).

    The same envelope the panel's `GET /messages/{id}` returns (ADR-0024), so the page's
    thread pane and the panel reader render through one component — plus this message's
    attachments and its full triage action set (mark toggle, Archive, Trash). The toggle
    flips to whichever state the message is *not* in.
    """
    toggle = _mark_read_action(message.id) if message.unread else _mark_unread_action(message.id)
    return {
        "subject": message.subject or "(no subject)",
        "from": message.sender,
        "date": message.date,
        "body": message.body or "",
        # The HTML body (ADR-0097, #627) — the shell renders it in a sandboxed iframe with
        # inline ``cid:`` images resolved through the module and remote images blocked by
        # default; ``body`` (text) stays the fallback for a text-only message.
        "body_html": message.body_html,
        "module": MODULE_NAME,
        "message_id": message.id,
        "unread": message.unread,
        "attachments": [att.model_dump() for att in message.attachments],
        "actions": [toggle, _archive_action(message.id), _trash_action(message.id)],
    }


async def build_mailbox_list(
    provider: MailProvider,
    *,
    mailbox: CachedMailbox | None = None,
    label: str | None = None,
    query: str | None = None,
    cursor: str | None = None,
    reconcile: bool = False,
    limit: int = MAILBOX_PAGE_SIZE,
) -> dict[str, Any]:
    """The `mailbox` list read (ADR-0087): the rail + one cursor page of threads.

    Browsing is folder-scoped (the active *label*); a *query* searches the whole mailbox
    (Gmail syntax, like ``mail_search``) while the rail keeps highlighting the current
    folder. Unread counts are filled only for Inbox (the nav-badge source) and the active
    label to bound the rail's provider calls. ``limit`` is clamped to the page cap so one
    fetch can't scan an unbounded mailbox (#539); the shell pages on with ``next_cursor``.

    Cache-first landing (ADR-0096, #623): when a *mailbox* orchestrator is supplied and this
    is the plain landing view (no *query*, first page), it serves from the local cache
    instantly — ``reconcile=True`` first pulls the provider delta into the cache. Search and
    deeper (*cursor*) pages bypass the cache and read the provider live, since the cache only
    materializes the default landing page.

    Args:
        provider: The active mail backend.
        mailbox: The cache orchestrator for the landing fast path (``None`` → always live).
        label: The rail selection; defaults to the Inbox.
        query: Optional provider-native search; searches all mail when present.
        cursor: Opaque next-page token from a previous read (``None`` for the first page).
        reconcile: On the cached landing path, pull the provider delta before serving.
        limit: Requested page size, clamped to :data:`MAILBOX_PAGE_SIZE`.
    """
    active = label or DEFAULT_LABEL
    capped = max(1, min(limit, MAILBOX_PAGE_SIZE))
    q = (query or "").strip() or None
    if mailbox is not None and q is None and not cursor:
        bundle = await (mailbox.reconcile(active) if reconcile else mailbox.landing(active))
        return {
            "title": "Mail",
            "labels": [lbl.model_dump() for lbl in bundle.labels],
            "active_label": active,
            "query": "",
            "threads": [thread.model_dump() for thread in bundle.threads],
            "next_cursor": bundle.next_cursor,
        }
    labels = await provider.list_labels(count_ids=(DEFAULT_LABEL, active))
    page = await provider.list_threads(
        label=None if q else active, query=q, cursor=cursor, limit=capped
    )
    return {
        "title": "Mail",
        "labels": [lbl.model_dump() for lbl in labels],
        "active_label": active,
        "query": q or "",
        "threads": [thread.model_dump() for thread in page.threads],
        "next_cursor": page.next_cursor,
    }


async def build_mailbox_thread(provider: MailProvider, thread_id: str) -> dict[str, Any]:
    """The `mailbox` thread read (ADR-0087): a full conversation + the reply prefill.

    Every message is rendered through the shared reader shape (:func:`message_payload`); the
    ``reply`` prefill derives the recipient/subject/threading from the **last** message via
    the tested :meth:`MailProvider.compose_reply` (#461) so a page reply threads correctly.
    The prefill carries only the last message's id — the actual send re-derives threading
    server-side, so the web never handles raw RFC-2822 headers.
    """
    thread = await provider.get_thread(thread_id)
    reply: dict[str, Any] | None = None
    if thread.messages:
        last = thread.messages[-1]
        composed = await provider.compose_reply(last.id, "")
        reply = {
            "reply_to_message_id": last.id,
            "to": composed.to,
            "subject": composed.subject,
            "reply_to_original": composed.reply_to_original
            or (f"{last.sender} — {thread.subject}" if last.sender else thread.subject),
        }
    return {
        "thread": {
            "id": thread.id,
            "subject": thread.subject,
            "messages": [message_payload(m) for m in thread.messages],
            "reply": reply,
        }
    }
