"""Domain-neutral mail contract (ADR-0016).

``MailProvider`` is the seam between the domain tools and any provider
implementation (Gmail, IMAP, Microsoft, …).  The domain model is provider-
agnostic; only the concrete provider knows about the underlying API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class MailMessage(BaseModel):
    """A mail message — provider-agnostic representation."""

    id: str
    thread_id: str
    subject: str
    sender: str
    to: list[str]
    date: str
    snippet: str
    body: str | None = None
    # Whether the message is unread. Provider-agnostic; the Gmail provider derives it
    # from the ``UNREAD`` label. Surfaced in the hover-card resolver (ADR-0019).
    unread: bool = False


class ComposedMessage(BaseModel):
    """A fully-composed outbound message — provider-agnostic (ADR-0085).

    This is both the draft the operator reviews in the split-pane *and* the exact content the
    transmit step sends, so what is approved is byte-for-byte what goes out. ``mail_send`` builds
    one directly from the model's arguments; ``mail_reply`` derives one via
    :meth:`MailProvider.compose_reply` (recipient/subject/threading from the original, #461). The
    reply-threading and ``reply_to_original`` fields are unset for a fresh ``mail_send``.
    """

    to: str
    subject: str
    body: str
    # Optional Cc — unused by the current tools (no Cc argument) but carried so the pane and a
    # future compose surface (#550) render/transmit it without a contract change.
    cc: str | None = None
    # RFC-2822 reply threading (#461), derived at compose time for a reply so Confirm need not
    # re-fetch the original: ``In-Reply-To`` / ``References`` headers + the provider thread id.
    in_reply_to: str | None = None
    references: str | None = None
    thread_id: str | None = None
    # The original message a reply answers (``sender — subject``), shown as the pane's thread
    # context. Presentation only — never placed on the wire.
    reply_to_original: str | None = None


class MailProvider(ABC):
    """Abstract mail provider — implemented by GmailProvider and future providers."""

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[MailMessage]:
        """Return messages matching *query* (metadata only; body is None)."""

    @abstractmethod
    async def read(self, message_id: str) -> MailMessage:
        """Return the full message, including decoded body."""

    @abstractmethod
    async def compose_reply(self, message_id: str, body: str) -> ComposedMessage:
        """Compose (but do **not** send) a reply to *message_id* in its existing thread.

        Derives the recipient and subject from the original message — replies to its sender
        (honoring ``Reply-To`` when present) with its subject (``Re: ...``, not doubled if
        already a reply) — and the RFC-2822 threading headers (``In-Reply-To`` / ``References``)
        plus the native thread id, so a later :meth:`transmit` lands in the same conversation on
        both ends (#461). The caller supplies only the new *body*. This is a **read** — it never
        transmits; the returned :class:`ComposedMessage` is the draft the operator reviews and,
        on Confirm, the exact content :meth:`transmit` sends (ADR-0085).
        """

    @abstractmethod
    async def transmit(self, message: ComposedMessage) -> str:
        """Send an already-composed *message* and return the sent message ID.

        The **only** transmitting method (ADR-0085). It is never reachable from an MCP tool —
        the module exposes it solely through its ``POST /send`` endpoint, which the core invokes
        after the operator confirms a draft. It sends *message* verbatim (including any reply
        threading), so the bytes sent match the draft that was reviewed.
        """

    @abstractmethod
    async def set_unread(self, message_id: str, unread: bool) -> None:
        """Set a message's read state: ``unread=False`` marks it read, ``True`` unread.

        Provider-agnostic counterpart to the ``unread`` flag on :class:`MailMessage`.
        Idempotent — setting the state a message is already in is a no-op.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True when the provider is reachable and the account is connected.

        A deep check — it may make a live provider API call. Prefer :meth:`is_available`
        for the polled status panel.
        """

    @abstractmethod
    async def is_available(self) -> bool:
        """Return True when an account is connected — a cheap credential check (#209).

        Unlike :meth:`health_check`, this must NOT make a live provider API call: it backs
        the status panel, which is polled, so a slow upstream can't stall the core's status
        proxy into a Bad Gateway. For Google providers it is a token-presence check.
        """
