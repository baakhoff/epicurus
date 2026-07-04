"""Mail module — MCP tool surface (ADR-0016).

Provider-agnostic tools: ``mail_search``, ``mail_read``, ``mail_send``, ``mail_reply``,
and the read-state pair ``mail_mark_read`` / ``mail_mark_unread``.
The tool names and signatures are domain-neutral; no Gmail specifics appear in the
tool surface (the manifest declares Gmail's OAuth scopes for the connect flow, #241).
``mail_search`` returns a :func:`~epicurus_core.tool_envelope` so the UI renders
each result as an entity-reference chip (ADR-0019): hover for the hover-card,
click to open the full message in the right-panel email-reader.
``mail_send`` and ``mail_reply`` are declared danger actions (ADR-0007): each sends
a real message and cannot be undone, so the web shell displays a confirmation prompt
before invoking either. ``mail_reply`` (#461) keeps the message in its existing
conversation thread — RFC-2822 ``In-Reply-To``/``References`` plus the provider's
native thread association — deriving the recipient and subject from the original
message rather than taking them as arguments.
``mail_mark_read`` / ``mail_mark_unread`` flip a message's read state; the
``email-reader`` panel also surfaces them as a tool-backed toggle (ADR-0024).
"""

from __future__ import annotations

import httpx

from epicurus_core import EntityRef, EpicurusModule, UiAction, UiSection, tool_envelope
from epicurus_mail.gmail import GMAIL_API_SCOPES
from epicurus_mail.provider import MailProvider

MODULE_NAME = "mail"

# Shown when ``messages.modify`` is rejected for lack of scope — the operator connected
# Google before mail required ``gmail.modify`` and must reconnect to grant it.
_SCOPE_HINT = (
    "Couldn't change the read state: the connected Google account is missing the Gmail"
    " modify permission. Reconnect Google (Settings → Connect) to grant it."
)


def build_module(provider: MailProvider) -> EpicurusModule:
    """Build the mail module and register its MCP tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.8.0",
        description=(
            "Provider-agnostic mail — search, read, send, and reply. Gmail is the v0.1 provider."
        ),
        ui=UiSection(
            icon="mail",
            summary=(
                "Lets the agent search, read, and send mail via your connected"
                " Google account. Requires Gmail scopes on the Google OAuth connection."
            ),
            status_url="/status",
            actions=[
                UiAction(
                    tool="mail_send",
                    label="Send mail",
                    description="Compose and send a message via the connected mail account.",
                    intent="danger",
                    confirm=(
                        "Send this message? This will deliver a real email and cannot be undone."
                    ),
                ),
                UiAction(
                    tool="mail_reply",
                    label="Reply",
                    description="Send a reply in the same conversation thread.",
                    intent="danger",
                    confirm=(
                        "Send this reply? This will deliver a real email and cannot be undone."
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
        text = f"Found {len(messages)} message(s):\n" + "\n".join(lines)
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
        """Send a mail message.

        **This action delivers a real message and cannot be undone.**
        Only invoke after explicit user confirmation of recipient, subject,
        and body.

        Args:
            to: Recipient email address.
            subject: Message subject line.
            body: Plain-text message body.

        Returns a confirmation string containing the sent message ID.
        """
        sent_id = await provider.send(to=to, subject=subject, body=body)
        return f"sent:{sent_id}"

    @module.tool()
    async def mail_reply(message_id: str, body: str) -> str:
        """Reply to a mail message, keeping it in the same conversation thread.

        **This action delivers a real message and cannot be undone.**
        Only invoke after explicit user confirmation of the reply body — the
        recipient and subject are derived from the original message (its
        sender, and its subject prefixed with "Re:" unless already a reply),
        so there is nothing else to compose.

        Args:
            message_id: The message being replied to (from ``mail_search`` or ``mail_read``).
            body: Plain-text reply body.

        Returns a confirmation string containing the sent message ID.
        """
        sent_id = await provider.reply(message_id, body)
        return f"sent:{sent_id}"

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
                return _SCOPE_HINT
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
                return _SCOPE_HINT
            raise
        return f"marked-unread:{message_id}"

    return module
