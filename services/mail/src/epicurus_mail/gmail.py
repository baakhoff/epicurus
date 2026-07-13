"""GmailProvider — Gmail API v1 backed by the platform OAuth token.

The provider fetches a valid access token via ``PlatformClient.get_oauth_token``
(which calls ``GET /platform/v1/oauth/google/token`` on the core).  It never
holds a client secret or refresh token.

Required Google OAuth scopes (requested when the operator connects via
``GET /platform/v1/oauth/google/connect?scope=...``):
    https://www.googleapis.com/auth/gmail.modify  (read + label writes, e.g. mark read/unread)
    https://www.googleapis.com/auth/gmail.send
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Sequence
from email.mime.text import MIMEText
from typing import Any

import httpx

from epicurus_core import PlatformClient
from epicurus_mail.provider import (
    AttachmentContent,
    ComposedMessage,
    MailAttachment,
    MailCursor,
    MailLabel,
    MailMessage,
    MailProvider,
    MailThread,
    MailThreadSummary,
    ThreadChanges,
    ThreadPage,
)

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"

# System labels shown in the mailbox rail, in this order (ADR-0087). Gmail exposes many more
# system labels (CATEGORY_*, CHAT, ...) that aren't useful folders; this curated set is the
# rail, with the operator's own ``user`` labels appended after.
_RAIL_SYSTEM_LABELS = ("INBOX", "STARRED", "SENT", "DRAFT", "IMPORTANT", "SPAM", "TRASH")
_SYSTEM_LABEL_TITLES = {
    "INBOX": "Inbox",
    "STARRED": "Starred",
    "SENT": "Sent",
    "DRAFT": "Drafts",
    "IMPORTANT": "Important",
    "SPAM": "Spam",
    "TRASH": "Trash",
}

# The Gmail API scopes this module needs (beyond the default identity scopes the core
# always requests). Declared in the manifest (``oauth_scopes``) so the shell requests them
# when connecting Google (#241).
# ``gmail.modify`` (not ``gmail.readonly``) is required to flip the ``UNREAD`` label via
# ``messages.modify`` (mark read/unread). It is a superset of read access, so it also backs
# search/read; ``gmail.send`` is kept explicit to document the send capability.
GMAIL_API_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Full scope set (identity + Gmail) the operator grants when connecting Google for mail.
GMAIL_REQUIRED_SCOPE = "openid email profile " + " ".join(GMAIL_API_SCOPES)


class GmailProvider(MailProvider):
    """Mail provider backed by the Gmail API v1.

    Args:
        platform: The typed platform client used to fetch OAuth tokens.
        tenant_id: The tenant whose token to retrieve.
    """

    def __init__(self, platform: PlatformClient, tenant_id: str) -> None:
        self._platform = platform
        self._tenant_id = tenant_id

    # ── internal helpers ─────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        return await self._platform.get_oauth_token("google")

    def _make_client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_GMAIL_API,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    @staticmethod
    async def _list_message_ids(
        client: httpx.AsyncClient, query: str, max_results: int
    ) -> list[str]:
        resp = await client.get(
            "/users/me/messages", params={"q": query, "maxResults": max_results}
        )
        resp.raise_for_status()
        return [m["id"] for m in resp.json().get("messages", [])]

    @staticmethod
    async def _fetch_message(
        client: httpx.AsyncClient, message_id: str, *, full: bool
    ) -> MailMessage:
        fmt = "full" if full else "metadata"
        params: dict[str, Any] = {"format": fmt}
        if not full:
            params["metadataHeaders"] = ["Subject", "From", "To", "Date"]
        resp = await client.get(f"/users/me/messages/{message_id}", params=params)
        resp.raise_for_status()
        return _parse_message(resp.json(), full=full)

    # ── MailProvider implementation ──────────────────────────────────────────

    async def search(self, query: str, max_results: int) -> list[MailMessage]:
        token = await self._get_token()
        async with self._make_client(token) as client:
            ids = await self._list_message_ids(client, query, max_results)
            return [await self._fetch_message(client, mid, full=False) for mid in ids]

    async def read(self, message_id: str) -> MailMessage:
        token = await self._get_token()
        async with self._make_client(token) as client:
            return await self._fetch_message(client, message_id, full=True)

    async def transmit(self, message: ComposedMessage) -> str:
        """Send an already-composed message (ADR-0085) — the sole transmitting path.

        Reachable only via the module's ``POST /send`` endpoint (the core calls it after the
        operator confirms a draft), never from an MCP tool. Sends *message* verbatim, honoring
        its reply threading (``In-Reply-To`` / ``References`` headers + the Gmail ``threadId``)
        when present, so a confirmed reply lands in the original conversation (#461).
        """
        raw = base64.urlsafe_b64encode(_build_mime(message).as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if message.thread_id:
            payload["threadId"] = message.thread_id
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.post("/users/me/messages/send", json=payload)
            resp.raise_for_status()
            return str(resp.json()["id"])

    @staticmethod
    async def _fetch_reply_headers(
        client: httpx.AsyncClient, message_id: str
    ) -> tuple[dict[str, str], str]:
        """The lowercased headers needed to build a reply, plus the Gmail ``threadId``.

        A metadata-only fetch (no body) — a reply doesn't quote the original by default,
        so there's nothing here beyond the headers this needs. ``Reply-To`` is fetched
        alongside ``From`` so the reply can honor it when present (#513).
        """
        resp = await client.get(
            f"/users/me/messages/{message_id}",
            params={
                "format": "metadata",
                "metadataHeaders": ["Message-ID", "References", "Subject", "From", "Reply-To"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        headers = {
            h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])
        }
        return headers, str(data.get("threadId", ""))

    async def compose_reply(self, message_id: str, body: str) -> ComposedMessage:
        """Compose a reply from the original's headers (#461, ADR-0085) — a read, never a send.

        Fetches the original message's threading headers (needs ``gmail.modify``) and derives
        the recipient, subject, and RFC-2822 threading. The returned :class:`ComposedMessage` is
        the draft the operator reviews and, on Confirm, exactly what :meth:`transmit` sends — so
        Confirm re-fetches nothing and cannot drift from what was shown.
        """
        token = await self._get_token()
        async with self._make_client(token) as client:
            # Single Gmail call on this path — so a 403/429 raised here is unambiguously the
            # header lookup, which is why mail_reply maps it to _SCOPE_HINT_REPLY_LOOKUP ("…look
            # up the original message"). If a future revision adds a *second* Gmail call to this
            # method, that one-size hint no longer fits every failure: split it by which call
            # raised rather than letting a new call silently inherit the lookup wording (#557).
            headers, thread_id = await self._fetch_reply_headers(client, message_id)
        return _compose_reply(headers, thread_id, body)

    async def set_unread(self, message_id: str, unread: bool) -> None:
        # Gmail tracks read state with the system ``UNREAD`` label: add it to mark unread,
        # remove it to mark read. ``messages.modify`` requires the ``gmail.modify`` scope.
        body = {"addLabelIds": ["UNREAD"]} if unread else {"removeLabelIds": ["UNREAD"]}
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.post(f"/users/me/messages/{message_id}/modify", json=body)
            resp.raise_for_status()

    # ── mailbox page (ADR-0087) ──────────────────────────────────────────────

    async def list_labels(self, *, count_ids: Sequence[str] = ()) -> list[MailLabel]:
        """The mailbox rail: the curated system labels then the operator's own (ADR-0087).

        Unread counts are filled only for *count_ids* (the active label + Inbox) via a
        per-label ``labels.get`` — Gmail's ``labels.list`` carries no counts, so filling
        every label would fan out to one call per label. Others stay ``None``.
        """
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.get("/users/me/labels")
            resp.raise_for_status()
            raw = resp.json().get("labels", [])
            labels = _order_labels(raw)
            wanted = {lid for lid in count_ids if lid}
            for label in labels:
                if label.id in wanted:
                    label.unread = await _label_unread(client, label.id)
            return labels

    async def list_threads(
        self, *, label: str | None, query: str | None, cursor: str | None, limit: int
    ) -> ThreadPage:
        """One cursor page of thread summaries for *label*/*query* (ADR-0087).

        ``threads.list`` gives ids + a page token; each thread then needs a metadata
        ``threads.get`` to build its row (subject/sender/date/unread/count). The page size
        bounds that fan-out so a single fetch can't scan an unbounded mailbox (#539).
        """
        params: dict[str, Any] = {"maxResults": limit}
        if label:
            params["labelIds"] = label
        if query:
            params["q"] = query
        if cursor:
            params["pageToken"] = cursor
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.get("/users/me/threads", params=params)
            resp.raise_for_status()
            data = resp.json()
            summaries = [
                await self._fetch_thread_summary(client, stub["id"])
                for stub in data.get("threads", [])
            ]
            return ThreadPage(threads=summaries, next_cursor=data.get("nextPageToken") or None)

    @staticmethod
    async def _fetch_thread_summary(client: httpx.AsyncClient, thread_id: str) -> MailThreadSummary:
        """One metadata ``threads.get`` -> a list row (ADR-0087).

        Shared by :meth:`list_threads` (a whole page) and :meth:`get_thread_summary` (one
        row an incremental reconcile needs to rebuild), so the two never derive a row
        differently.
        """
        detail = await client.get(
            f"/users/me/threads/{thread_id}",
            params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
        )
        detail.raise_for_status()
        return _thread_summary(detail.json())

    async def get_thread(self, thread_id: str) -> MailThread:
        """The full conversation — every message with body + attachment metadata (ADR-0087)."""
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.get(f"/users/me/threads/{thread_id}", params={"format": "full"})
            resp.raise_for_status()
            data = resp.json()
            messages = [_parse_message(m, full=True) for m in data.get("messages", [])]
            subject = messages[0].subject if messages else "(no subject)"
            return MailThread(id=thread_id, subject=subject, messages=messages)

    async def archive(self, message_id: str) -> None:
        """Drop the ``INBOX`` label so the message leaves the Inbox (ADR-0087). Idempotent."""
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.post(
                f"/users/me/messages/{message_id}/modify", json={"removeLabelIds": ["INBOX"]}
            )
            resp.raise_for_status()

    async def trash(self, message_id: str) -> None:
        """Move the message to Trash (recoverable, not a permanent delete) (ADR-0087)."""
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.post(f"/users/me/messages/{message_id}/trash")
            resp.raise_for_status()

    async def get_attachment(self, message_id: str, attachment_id: str) -> AttachmentContent:
        """Fetch one attachment's bytes + metadata for the core to stream (ADR-0087).

        Two calls: the message (to resolve the part's filename/mime — ``attachments.get``
        returns neither) then the attachment bytes. Nothing is persisted by the module.
        """
        token = await self._get_token()
        async with self._make_client(token) as client:
            msg = await client.get(f"/users/me/messages/{message_id}", params={"format": "full"})
            msg.raise_for_status()
            part = _find_attachment_part(msg.json().get("payload", {}), attachment_id)
            if part is None:
                raise httpx.HTTPStatusError(
                    "attachment not found",
                    request=msg.request,
                    response=httpx.Response(404, request=msg.request),
                )
            att = await client.get(f"/users/me/messages/{message_id}/attachments/{attachment_id}")
            att.raise_for_status()
            raw = att.json().get("data", "")
            content = base64.urlsafe_b64decode(raw + "==") if raw else b""
            return AttachmentContent(
                filename=part.filename or "attachment",
                mime_type=part.mime_type or "application/octet-stream",
                content=content,
            )

    # ── incremental sync (ADR-0096, #623) ────────────────────────────────────

    async def current_cursor(self) -> MailCursor:
        """The mailbox's ``historyId`` right now, via ``users.getProfile`` (ADR-0096).

        One cheap call; Gmail returns ``historyId`` as a string, coerced to the ``int`` the
        cache stores as ``BigInteger`` (it is ~1e10+ and climbs, so never an int32 column).
        """
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.get("/users/me/profile")
            resp.raise_for_status()
            return MailCursor(history_id=_as_int(resp.json().get("historyId")))

    async def changed_threads_since(self, cursor: MailCursor) -> ThreadChanges | None:
        """Threads touched since *cursor* via ``users.history.list`` (ADR-0096, #623).

        Replays every change after ``cursor.history_id`` (messages added/deleted, labels
        added/removed) and collects each referenced ``threadId`` — so a reconcile rebuilds
        only those rows. Paginates ``nextPageToken`` to the end. A **404** means the start
        history is older than Gmail retains (~a week) → returns ``None`` so the caller does a
        full resync. The advanced cursor is the response's own ``historyId`` (the mailbox top),
        falling back to *cursor* when Gmail omits it.
        """
        start = cursor.history_id
        if start is None:  # never synced — the orchestrator should full-sync, not diff
            return None
        token = await self._get_token()
        changed: set[str] = set()
        latest = start
        page_token: str | None = None
        async with self._make_client(token) as client:
            while True:
                params: dict[str, Any] = {"startHistoryId": start}
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get("/users/me/history", params=params)
                if resp.status_code == httpx.codes.NOT_FOUND:
                    return None  # history expired → caller full-resyncs
                resp.raise_for_status()
                data = resp.json()
                for record in data.get("history", []):
                    changed.update(_history_thread_ids(record))
                latest = _as_int(data.get("historyId")) or latest
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        return ThreadChanges(changed_thread_ids=changed, next_cursor=MailCursor(history_id=latest))

    async def get_thread_summary(self, thread_id: str) -> MailThreadSummary | None:
        """One thread's list row, or ``None`` if it was deleted (a 404) (ADR-0096, #623)."""
        token = await self._get_token()
        async with self._make_client(token) as client:
            try:
                return await self._fetch_thread_summary(client, thread_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == httpx.codes.NOT_FOUND:
                    return None
                raise

    async def health_check(self) -> bool:
        try:
            token = await self._get_token()
            async with self._make_client(token) as client:
                resp = await client.get("/users/me/profile")
                return resp.status_code == 200
        except Exception:
            return False

    async def is_available(self) -> bool:
        """True when a Google token is available for this tenant (#209).

        A token-presence check — a fast core round-trip via the OAuth vault, not a live
        Gmail API call — so the polled status panel can't stall the core's status proxy
        into a Bad Gateway. Any HTTP failure (not connected, or the core unreachable)
        reads as not available.
        """
        try:
            await self._get_token()
            return True
        except httpx.HTTPError:
            return False


def _parse_message(data: dict[str, Any], *, full: bool) -> MailMessage:
    """Convert a Gmail API message object to a ``MailMessage``."""
    payload = data.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    to_raw = headers.get("to", "")
    to_list = [t.strip() for t in to_raw.split(",") if t.strip()]
    body: str | None = None
    body_html: str | None = None
    attachments: list[MailAttachment] = []
    if full:
        body = _extract_body(payload)
        body_html = _extract_html(payload)
        attachments = _extract_attachments(payload)
    # Gmail flags an unread message with the system ``UNREAD`` label; ``labelIds`` is
    # returned for both the ``metadata`` and ``full`` formats, so search and read agree.
    unread = "UNREAD" in data.get("labelIds", [])
    return MailMessage(
        id=data["id"],
        thread_id=data.get("threadId", ""),
        subject=headers.get("subject", "(no subject)"),
        sender=headers.get("from", ""),
        to=to_list,
        date=headers.get("date", ""),
        snippet=data.get("snippet", ""),
        body=body,
        body_html=body_html,
        unread=unread,
        attachments=attachments,
    )


def _order_labels(raw: list[dict[str, Any]]) -> list[MailLabel]:
    """Curated system labels (in rail order) then the operator's own, from ``labels.list``.

    Gmail exposes many non-folder system labels (``CATEGORY_*``, ``CHAT``, ``UNREAD`` ...);
    only :data:`_RAIL_SYSTEM_LABELS` are useful folders, so the rail shows those first (in a
    fixed, familiar order) and appends every ``user`` label alphabetically.
    """
    by_id = {lbl.get("id"): lbl for lbl in raw}
    labels: list[MailLabel] = []
    for lid in _RAIL_SYSTEM_LABELS:
        if lid in by_id:
            labels.append(MailLabel(id=lid, title=_SYSTEM_LABEL_TITLES[lid], kind="system"))
    user = sorted(
        (lbl for lbl in raw if lbl.get("type") == "user"),
        key=lambda lbl: str(lbl.get("name", "")).lower(),
    )
    for lbl in user:
        labels.append(MailLabel(id=str(lbl["id"]), title=str(lbl.get("name", "")), kind="user"))
    return labels


async def _label_unread(client: httpx.AsyncClient, label_id: str) -> int | None:
    """The unread count for one label via ``labels.get`` — ``None`` if it can't be read.

    Best-effort: a label that 404s (renamed/removed between the list and this call) or any
    transient error yields ``None`` (no count shown) rather than failing the whole rail.
    """
    try:
        resp = await client.get(f"/users/me/labels/{label_id}")
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    value = resp.json().get("messagesUnread")
    return int(value) if isinstance(value, int) else None


def _headers_of(message: dict[str, Any]) -> dict[str, str]:
    """Lowercased ``{header: value}`` for one Gmail message dict (empty if none)."""
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}


def _thread_summary(data: dict[str, Any]) -> MailThreadSummary:
    """Build a thread-list row from a metadata ``threads.get`` (ADR-0087).

    Subject comes from the first message (the conversation's), while the sender/snippet/date
    reflect the **most recent** message (what a mail client shows in the row); the thread is
    unread if any message is.
    """
    messages = data.get("messages", [])
    first = messages[0] if messages else {}
    last = messages[-1] if messages else {}
    first_headers = _headers_of(first)
    last_headers = _headers_of(last)
    unread = any("UNREAD" in m.get("labelIds", []) for m in messages)
    # The thread belongs to every label any of its messages carries (Gmail thread-label
    # semantics), so a reconcile can tell if it still sits in a cached folder (ADR-0096).
    label_ids = sorted({lid for m in messages for lid in m.get("labelIds", [])})
    # Order by the newest message's internalDate (epoch ms) — Gmail's own thread ordering.
    sort_ts = _as_int(last.get("internalDate")) or 0
    return MailThreadSummary(
        id=str(data.get("id", "")),
        subject=first_headers.get("subject", "(no subject)"),
        sender=last_headers.get("from", ""),
        snippet=last.get("snippet", "") or data.get("snippet", ""),
        date=last_headers.get("date", ""),
        unread=unread,
        message_count=len(messages),
        sort_ts=sort_ts,
        label_ids=label_ids,
    )


def _as_int(value: Any) -> int | None:
    """Coerce Gmail's string ``historyId`` (and kin) to ``int``; ``None`` on missing/garbage.

    Gmail serializes ``historyId`` as a decimal string (``"987654"``); the cache stores it as
    a ``BigInteger``. Defensive so a malformed body can't crash a reconcile with ``ValueError``.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _history_thread_ids(record: dict[str, Any]) -> set[str]:
    """Every ``threadId`` referenced by one ``users.history.list`` record (ADR-0096, #623).

    A record carries the affected messages under ``messages`` plus the typed change arrays
    (``messagesAdded`` / ``messagesDeleted`` / ``labelsAdded`` / ``labelsRemoved``), each entry
    wrapping a ``message`` object. Walking all of them catches every thread whose row may now be
    stale — a new/removed message *or* a flag flip (read/unread, archived) — which is the set the
    reconcile rebuilds.
    """
    ids: set[str] = set()
    for message in record.get("messages", []):
        if isinstance(message, dict) and message.get("threadId"):
            ids.add(str(message["threadId"]))
    for key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
        for entry in record.get(key, []):
            message = entry.get("message", {}) if isinstance(entry, dict) else {}
            if isinstance(message, dict) and message.get("threadId"):
                ids.add(str(message["threadId"]))
    return ids


def _part_headers(part: dict[str, Any]) -> dict[str, str]:
    """Lowercased ``{header: value}`` for one Gmail payload *part* (empty if none)."""
    return {h["name"].lower(): h["value"] for h in part.get("headers", [])}


def _extract_attachments(payload: dict[str, Any]) -> list[MailAttachment]:
    """Walk a Gmail payload for attachment parts (ADR-0097, #627).

    Includes a part with a body ``attachmentId`` and **either** a filename (an ordinary
    attachment) **or** a ``Content-ID`` (an inline image an HTML body references as
    ``cid:<id>``). ``content_id`` is the header stripped of its angle brackets; ``inline`` is
    set when the part is dispositioned inline or carries a ``Content-ID`` — the shell resolves
    those for the HTML body and keeps them out of the download row.
    """
    found: list[MailAttachment] = []

    def walk(part: dict[str, Any]) -> None:
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        filename = part.get("filename") or ""
        headers = _part_headers(part)
        content_id = headers.get("content-id", "").strip().strip("<>").strip() or None
        disposition = headers.get("content-disposition", "").lower()
        is_inline = "inline" in disposition or content_id is not None
        if attachment_id and (filename or content_id):
            found.append(
                MailAttachment(
                    id=str(attachment_id),
                    filename=filename or content_id or "inline",
                    mime_type=str(part.get("mimeType", "")),
                    size=int(body.get("size", 0) or 0),
                    content_id=content_id,
                    inline=is_inline,
                )
            )
        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    return found


def _extract_html(payload: dict[str, Any]) -> str | None:
    """The message's raw ``text/html`` body part, decoded, or ``None`` (ADR-0097, #627).

    Unlike :func:`_extract_body` (which decodes HTML *to text* as a fallback), this returns the
    HTML verbatim for the shell to render in a sandboxed iframe. Safety lives at the render
    boundary (the sandbox + the shell's inert-parse sanitize), not here.
    """
    return _first_part_text(payload, "text/html")


def _find_attachment_part(payload: dict[str, Any], attachment_id: str) -> MailAttachment | None:
    """The attachment part matching *attachment_id* (for filename/mime), or ``None``."""
    for att in _extract_attachments(payload):
        if att.id == attachment_id:
            return att
    return None


def _reply_subject(original_subject: str) -> str:
    """``Re: <subject>`` — unless *original_subject* is already a reply (avoids ``Re: Re: …``).

    A missing subject reads as ``(no subject)``, mirroring ``_parse_message``'s own fallback,
    rather than producing a bare ``Re:`` with nothing after it.
    """
    subject = original_subject or "(no subject)"
    return subject if subject.strip().lower().startswith("re:") else f"Re: {subject}"


def _compose_reply(headers: dict[str, str], thread_id: str, body: str) -> ComposedMessage:
    """Derive the reply :class:`ComposedMessage` from a message's lowercased *headers* (#461).

    This is the *composition* half of what used to be one send call (ADR-0085): it resolves the
    recipient, subject, and threading but transmits nothing — :meth:`GmailProvider.transmit`
    later sends the returned message verbatim, after the operator confirms it.

    Threads via RFC-2822 headers (#461): ``In-Reply-To`` is the original's ``Message-ID``;
    ``References`` is the original's own reference chain plus that same ``Message-ID`` (RFC
    2822 recommends the full chain, not just the immediate parent), so the reply threads
    correctly even in mail clients that ignore Gmail's own ``threadId``.

    The recipient honors ``Reply-To`` over ``From`` when the original carries one (#513) —
    mailing lists, newsletters, and support desks commonly set ``Reply-To`` to route replies
    away from the sending address, and addressing ``From`` in that case sends the reply
    somewhere the sender never intended it to land. ``Reply-To`` is stripped before that
    check (#538): a whitespace-only value (some senders emit ``Reply-To: `` with nothing
    after it) is still a non-empty string, which Python treats as truthy, so an unstripped
    check would "win" with blank whitespace and produce an unroutable ``To``.

    No self-reply guard: replying to a message the operator sent themselves addresses the
    operator (``Reply-To``/``From`` both resolve back to their own account). Deliberately
    left unguarded (#513) — it is indistinguishable from legitimately mailing yourself a note,
    and every reply is now shown in the split-pane for Confirm/Decline (ADR-0085) so nothing
    fires without the operator seeing the recipient first; detecting it would need an extra
    profile lookup per reply for a case with no clear wrong answer to guard against.
    """
    reply_to = headers.get("reply-to", "").strip()
    original_message_id = headers.get("message-id", "")
    references = " ".join(filter(None, [headers.get("references", ""), original_message_id]))
    sender = headers.get("from", "")
    original_subject = headers.get("subject", "") or "(no subject)"
    return ComposedMessage(
        to=reply_to or sender,
        subject=_reply_subject(headers.get("subject", "")),
        body=body,
        in_reply_to=original_message_id or None,
        references=references or None,
        thread_id=thread_id or None,
        reply_to_original=f"{sender} — {original_subject}" if sender else original_subject,
    )


def _build_mime(message: ComposedMessage) -> MIMEText:
    """Assemble the outgoing MIME for an already-composed *message* (the transmit half, #461).

    Sets ``In-Reply-To`` / ``References`` only when the message carries reply threading, so a
    fresh ``mail_send`` produces a plain message and a ``mail_reply`` threads correctly.
    """
    msg = MIMEText(message.body, "plain", "utf-8")
    msg["To"] = message.to
    if message.cc:
        msg["Cc"] = message.cc
    msg["Subject"] = message.subject
    if message.in_reply_to:
        msg["In-Reply-To"] = message.in_reply_to
    if message.references:
        msg["References"] = message.references
    return msg


def _first_part_text(payload: dict[str, Any], mime: str) -> str | None:
    """Recursively decode the first body part whose MIME type is *mime*, or ``None``."""
    if payload.get("mimeType") == mime:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _first_part_text(part, mime)
        if result is not None:
            return result
    return None


def _extract_body(payload: dict[str, Any]) -> str | None:
    """The message body as **text**: the first ``text/plain`` part, else HTML->text (ADR-0087).

    Plain-text-first — no HTML is ever rendered in the shell, so there is no HTML-mail XSS
    surface. When a message carries only an HTML part (common for newsletters), it is
    stripped to readable text server-side (:func:`_html_to_text`) rather than shown blank.
    """
    plain = _first_part_text(payload, "text/plain")
    if plain is not None:
        return plain
    html_body = _first_part_text(payload, "text/html")
    if html_body is not None:
        return _html_to_text(html_body)
    return None


# HTML -> text (ADR-0087). Order is the security property: script/style *content* is removed
# first, block tags become newlines, ALL remaining tags (with their attributes) are stripped,
# and only THEN are entities decoded — so a decoded ``&lt;script&gt;`` becomes inert literal
# text, never re-parsed as a tag (the output is rendered as plain text, never as HTML).
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|head|title)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_BREAK_RE = re.compile(r"(?i)<\s*(?:br|/p|/div|/tr|/li|/h[1-6]|/table)\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n[ \t]*\n+")


def _html_to_text(raw: str) -> str:
    """Strip HTML to readable text — the server-side "sanitizer" for HTML-only mail (ADR-0087).

    Not a renderer: it never emits HTML. It removes ``script``/``style``/``head`` blocks
    (content and all), turns block-level tags into line breaks, strips every remaining tag
    **including its attributes** (so ``onerror=`` / ``href=`` never survive), then decodes
    entities last — so nothing that decodes can be re-interpreted as markup. Adversarial
    fixtures pin these properties.
    """
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace(chr(0xA0), " ")  # decoded &nbsp; -> ordinary space
    text = _INLINE_WS_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
