"""Mail module — MCP tool surface (ADR-0016).

Three provider-agnostic tools: ``mail_search``, ``mail_read``, ``mail_send``.
The tool names and signatures are domain-neutral; no Gmail specifics appear here.
``mail_send`` is declared a danger action (ADR-0007): it sends a real message
and cannot be undone, so the web shell displays a confirmation prompt before
invoking it.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, UiAction, UiSection
from epicurus_mail.provider import MailMessage, MailProvider

MODULE_NAME = "mail"


def build_module(provider: MailProvider) -> EpicurusModule:
    """Build the mail module and register its MCP tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
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
    )

    module.emits("mail.sent", "Published after a message is sent successfully.")

    @module.tool()
    async def mail_search(query: str, max_results: int = 10) -> list[MailMessage]:
        """Search for mail matching *query*.

        Supports the same query syntax as Gmail (e.g. ``from:alice``,
        ``subject:invoice``, ``is:unread``).  Returns up to *max_results*
        messages with metadata only — no body.  Call ``mail_read`` with a
        message ``id`` to retrieve the full body.

        Args:
            query: Mail search expression (Gmail query syntax).
            max_results: Maximum number of messages to return (1-50, default 10).
        """
        capped = max(1, min(max_results, 50))
        return await provider.search(query, capped)

    @module.tool()
    async def mail_read(message_id: str) -> MailMessage:
        """Fetch the full content of a mail message by its *message_id*.

        Returns the message with the decoded plain-text body.  Use
        ``mail_search`` first to discover message IDs.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        return await provider.read(message_id)

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
