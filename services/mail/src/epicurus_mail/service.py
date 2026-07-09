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

import httpx

from epicurus_core import (
    EntityRef,
    EpicurusModule,
    UiAction,
    UiSection,
    capped_listing,
    draft_review,
    tool_envelope,
)
from epicurus_mail.gmail import GMAIL_API_SCOPES
from epicurus_mail.provider import ComposedMessage, MailProvider

MODULE_NAME = "mail"

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

# Shown when mail_reply's own metadata lookup (the original message's Reply-To/From/
# Subject/Message-ID — needs gmail.modify) is rejected for lack of scope. Distinct wording
# from _SCOPE_HINT: that constant talks about "the read state", which doesn't apply here —
# the reply was never composed, let alone sent (#538).
_SCOPE_HINT_REPLY_LOOKUP = (
    "Couldn't reply: the connected Google account is missing the Gmail modify permission"
    " needed to look up the original message. Reconnect Google (Settings → Connect) to"
    " grant it."
)

# Gmail returns 403 both for a missing OAuth scope and for per-user/per-day rate limiting
# (``usageLimits``) — the reasons below are Google's Discovery-API error codes for the
# latter. Blaming every 403 on a missing scope misdirects an operator who is simply being
# throttled (#538).
_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}
)

_RATE_LIMIT_HINT = (
    "Gmail is rate-limiting this account (too many requests in a short time, or the daily"
    " quota was reached). Wait a bit and try again."
)


def _describe_403(exc: httpx.HTTPStatusError, scope_hint: str) -> str:
    """The user-facing hint for a Gmail 403: *scope_hint* unless the error body names one
    of :data:`_RATE_LIMIT_REASONS`, in which case that's the real cause (#538). Falls back
    to *scope_hint* whenever the body doesn't parse into Google's error shape too, since a
    missing scope remains the far more common cause of an unparseable 403.
    """
    try:
        reason = exc.response.json()["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return scope_hint
    return _RATE_LIMIT_HINT if reason in _RATE_LIMIT_REASONS else scope_hint


def _draft_summary(message: ComposedMessage) -> str:
    """A one-line label for a composed draft — shown in the turn activity and logs (ADR-0085)."""
    return f"Email to {message.to} — {message.subject or '(no subject)'}"


def build_module(provider: MailProvider) -> EpicurusModule:
    """Build the mail module and register its MCP tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.9.0",
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
        messages = await provider.search(query, capped)
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
        m = await provider.read(message_id)
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
            if exc.response.status_code == 403:
                return _describe_403(exc, _SCOPE_HINT_REPLY_LOOKUP)
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
            if exc.response.status_code == 403:
                return _describe_403(exc, _SCOPE_HINT)
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
            if exc.response.status_code == 403:
                return _describe_403(exc, _SCOPE_HINT)
            raise
        return f"marked-unread:{message_id}"

    return module
