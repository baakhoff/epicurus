"""Mail module — MCP tool surface (ADR-0016).

Three provider-agnostic tools: ``mail_search``, ``mail_read``, ``mail_send``.
The tool names and signatures are domain-neutral; no Gmail specifics appear here.
``mail_search`` returns a :func:`~epicurus_core.tool_envelope` so the UI renders
each result as an entity-reference chip (ADR-0019): hover for the hover-card,
click to open the full message in the right-panel email-reader.
``mail_send`` is declared a danger action (ADR-0007): it sends a real message
and cannot be undone, so the web shell displays a confirmation prompt before
invoking it.
"""

from __future__ import annotations

from epicurus_core import EntityRef, EpicurusModule, UiAction, UiSection, tool_envelope
from epicurus_mail.provider import MailProvider

MODULE_NAME = "mail"


def build_module(provider: MailProvider) -> EpicurusModule:
    """Build the mail module and register its MCP tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.5.0",
        description=(
            "Provider-agnostic mail — search, read, and send. Gmail is the v0.1 provider."
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
            ],
        ),
        resolver=True,
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

    return module
