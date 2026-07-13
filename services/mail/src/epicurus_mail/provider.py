"""Domain-neutral mail contract (ADR-0016).

``MailProvider`` is the seam between the domain tools and any provider
implementation (Gmail, IMAP, Microsoft, …).  The domain model is provider-
agnostic; only the concrete provider knows about the underlying API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from pydantic import BaseModel, Field


class MailAttachment(BaseModel):
    """One attachment on a message — metadata only; bytes are streamed separately (ADR-0087).

    The mailbox page lists these under a message and downloads one on demand via the core
    attachment proxy (:meth:`MailProvider.get_attachment`). The module never stores the bytes.
    """

    id: str
    filename: str
    mime_type: str = ""
    size: int = 0


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
    # Attachments carried by the message (ADR-0087). Populated only on a full read
    # (``read`` / ``get_thread``); a metadata-only search/list leaves it empty.
    attachments: list[MailAttachment] = Field(default_factory=list)


class MailLabel(BaseModel):
    """A folder/label in the mailbox rail (ADR-0087) — provider-agnostic.

    ``kind`` is ``"system"`` for provider-defined labels (Inbox, Sent, ...) and ``"user"``
    for the operator's own. ``unread`` is the label's unread count when the provider
    supplies it cheaply, else ``None`` (the rail renders a count only when present, so a
    provider that can't count per-label capability-gates rather than forcing a number).
    """

    id: str
    title: str
    kind: str = "system"
    unread: int | None = None


class MailThreadSummary(BaseModel):
    """One row in the paginated thread list (ADR-0087) — a conversation at a glance."""

    id: str
    subject: str
    sender: str
    snippet: str
    date: str
    unread: bool = False
    message_count: int = 1
    # The thread's last-message time as epoch **milliseconds** — the cache's ordering key
    # (ADR-0096, #623), provider-neutral (Gmail ``internalDate``; IMAP ``INTERNALDATE``).
    # 0 when the provider didn't supply one; such rows sort last.
    sort_ts: int = 0
    # The folders/labels this thread is filed under (Gmail label ids; IMAP folders / JMAP
    # mailboxes map the same way) — provider-neutral. Lets an incremental reconcile decide
    # whether a changed thread still belongs to a cached folder without a second fetch. Empty
    # on a plain list read (the query already scoped the folder); populated by
    # :meth:`MailProvider.get_thread_summary`.
    label_ids: list[str] = Field(default_factory=list)


class ThreadPage(BaseModel):
    """A cursor-paginated page of thread summaries (ADR-0087).

    ``next_cursor`` is the opaque provider token for the following page, or ``None`` at the
    end — cursor pagination only (never offset), since a mailbox is unbounded (#539).
    """

    threads: list[MailThreadSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class MailThread(BaseModel):
    """A full conversation — every message in the thread, oldest first (ADR-0087)."""

    id: str
    subject: str
    messages: list[MailMessage] = Field(default_factory=list)


class AttachmentContent(BaseModel):
    """The bytes + metadata for one downloaded attachment (ADR-0087).

    Returned by :meth:`MailProvider.get_attachment`; the core streams ``content`` to the
    browser with ``mime_type`` / ``filename``. Never persisted by the module.
    """

    filename: str
    mime_type: str
    content: bytes


class MailCursor(BaseModel):
    """A provider-neutral change cursor for incremental sync (ADR-0096, #623).

    The local cache persists this opaque token and hands it back to
    :meth:`MailProvider.changed_threads_since` to pull only the delta since it was taken.
    Each provider fills the field(s) it uses and ignores the rest:

    - **Gmail** uses ``history_id`` — the mailbox's monotonic ``historyId`` (from
      ``users.getProfile`` / any message). ``users.history.list`` replays every change
      after it.
    - **IMAP** (future) uses ``uid_validity`` + ``uid_next`` — a folder's ``UIDVALIDITY``
      pins the UID namespace (a rotation invalidates every cached UID → full resync) and
      ``UIDNEXT`` bounds the highest UID seen, so a later ``UID FETCH`` pulls only newer
      messages.

    An all-``None`` cursor means "never synced" (a cold cache), which the orchestrator
    treats as a full sync.
    """

    history_id: int | None = None
    uid_validity: int | None = None
    uid_next: int | None = None

    def is_empty(self) -> bool:
        """True when no provider has stamped this cursor yet (a cold cache)."""
        return self.history_id is None and self.uid_validity is None and self.uid_next is None


class ThreadChanges(BaseModel):
    """The delta a provider reports since a :class:`MailCursor` (ADR-0096, #623).

    Thread-granular because the cache materializes *thread rows*: ``changed_thread_ids`` is
    every thread touched by any message added/removed or (un)labeled since the cursor, so the
    orchestrator re-derives exactly those rows (a single ``get_thread_summary`` each) and
    leaves the rest of the cache untouched — the "pull only the delta" property. ``next_cursor``
    is the advanced cursor to persist after the delta is applied.

    A provider returns ``None`` from :meth:`MailProvider.changed_threads_since` (not an empty
    ``ThreadChanges``) when the cursor is too old to replay — Gmail expires history after a
    week, an IMAP ``UIDVALIDITY`` rotation drops the namespace — signalling the orchestrator to
    fall back to a full resync. An empty ``changed_thread_ids`` with a fresh ``next_cursor``
    means "nothing changed" (the cheap common case).
    """

    changed_thread_ids: set[str] = Field(default_factory=set)
    next_cursor: MailCursor = Field(default_factory=MailCursor)


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

    # ── mailbox page (ADR-0087) ──────────────────────────────────────────────

    @abstractmethod
    async def list_labels(self, *, count_ids: Sequence[str] = ()) -> list[MailLabel]:
        """Return the mailbox's folders/labels for the page's rail (ADR-0087).

        System labels (Inbox, Sent, ...) first, then the operator's own. *count_ids* names
        the labels whose unread count the caller wants filled — kept small (the active label
        + Inbox) so the rail's per-label counts don't fan out to one call per label. A label
        outside *count_ids*, or a provider that can't count cheaply, leaves
        :attr:`MailLabel.unread` ``None`` (capability-gate, not a forced zero — ADR-0030).
        """

    @abstractmethod
    async def list_threads(
        self, *, label: str | None, query: str | None, cursor: str | None, limit: int
    ) -> ThreadPage:
        """Return one cursor-paginated page of thread summaries (ADR-0087).

        Scoped to *label* (the rail selection) and/or a provider-native *query* (Gmail
        syntax, as ``search`` uses). *cursor* is the opaque token from a previous page's
        ``next_cursor`` (``None`` for the first page); *limit* bounds the page — the caller
        caps it so one fetch can't scan an unbounded mailbox (#539).
        """

    @abstractmethod
    async def get_thread(self, thread_id: str) -> MailThread:
        """Return the full conversation *thread_id* — every message, oldest first (ADR-0087).

        Each message carries its decoded body and attachment metadata, so the page's thread
        pane renders the whole conversation from one call.
        """

    @abstractmethod
    async def archive(self, message_id: str) -> None:
        """Archive a message — remove it from the Inbox without deleting it (ADR-0087).

        Idempotent. For Gmail this drops the ``INBOX`` label (``messages.modify``), inside
        the already-granted ``gmail.modify`` scope — no reconnect.
        """

    @abstractmethod
    async def trash(self, message_id: str) -> None:
        """Move a message to Trash (ADR-0087) — recoverable, not a permanent delete.

        Idempotent. Permanent deletion is deliberately out of scope (it needs the full
        Gmail scope); trash is inside ``gmail.modify``.
        """

    @abstractmethod
    async def get_attachment(self, message_id: str, attachment_id: str) -> AttachmentContent:
        """Fetch one attachment's bytes + metadata for the core to stream (ADR-0087).

        The bytes are never persisted by the module — they flow provider -> module ->
        core proxy -> browser. Raises if the message or attachment does not exist.
        """

    # ── incremental sync (ADR-0096, #623) ────────────────────────────────────

    @abstractmethod
    async def current_cursor(self) -> MailCursor:
        """The mailbox's change cursor *right now* — the top of the change log (ADR-0096).

        A cheap read (Gmail: one ``users.getProfile``). Stamped into the cache after a full
        sync so the next reconcile can ask :meth:`changed_threads_since` for just the delta.
        """

    @abstractmethod
    async def changed_threads_since(self, cursor: MailCursor) -> ThreadChanges | None:
        """Which threads changed since *cursor*, and the advanced cursor (ADR-0096, #623).

        Returns a thread-granular :class:`ThreadChanges` so the caller re-derives only the
        affected rows. Returns ``None`` when *cursor* is too old to replay (Gmail history
        expired, or an IMAP ``UIDVALIDITY`` rotation) — the caller then does a full resync.
        An empty delta with a fresh ``next_cursor`` is the cheap "nothing changed" case.
        """

    @abstractmethod
    async def get_thread_summary(self, thread_id: str) -> MailThreadSummary | None:
        """One thread's list-row summary (ADR-0096, #623), or ``None`` if it no longer exists.

        The single-thread counterpart to :meth:`list_threads`, used by an incremental
        reconcile to rebuild exactly the rows a delta touched. ``None`` means the thread was
        deleted (its cached row should be dropped).
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
