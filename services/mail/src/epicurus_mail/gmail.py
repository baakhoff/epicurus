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
from email.mime.text import MIMEText
from typing import Any

import httpx

from epicurus_core import PlatformClient
from epicurus_mail.provider import ComposedMessage, MailMessage, MailProvider

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"

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
    headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
    to_raw = headers.get("to", "")
    to_list = [t.strip() for t in to_raw.split(",") if t.strip()]
    body: str | None = None
    if full:
        body = _extract_body(data.get("payload", {}))
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
        unread=unread,
    )


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


def _extract_body(payload: dict[str, Any]) -> str | None:
    """Recursively extract the first plain-text body part from a Gmail payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result is not None:
            return result
    return None
