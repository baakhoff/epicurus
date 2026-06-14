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


class MailProvider(ABC):
    """Abstract mail provider — implemented by GmailProvider and future providers."""

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[MailMessage]:
        """Return messages matching *query* (metadata only; body is None)."""

    @abstractmethod
    async def read(self, message_id: str) -> MailMessage:
        """Return the full message, including decoded body."""

    @abstractmethod
    async def send(self, to: str, subject: str, body: str) -> str:
        """Send a message and return the sent message ID."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True when the provider is reachable and the account is connected."""
