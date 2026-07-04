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
from epicurus_mail.provider import MailMessage, MailProvider

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

    async def send(self, to: str, subject: str, body: str) -> str:
        msg = MIMEText(body, "plain", "utf-8")
        msg["To"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        token = await self._get_token()
        async with self._make_client(token) as client:
            resp = await client.post("/users/me/messages/send", json={"raw": raw})
            resp.raise_for_status()
            return str(resp.json()["id"])

    @staticmethod
    async def _fetch_reply_headers(
        client: httpx.AsyncClient, message_id: str
    ) -> tuple[dict[str, str], str]:
        """The lowercased headers needed to build a reply, plus the Gmail ``threadId``.

        A metadata-only fetch (no body) — a reply doesn't quote the original by default,
        so there's nothing here beyond the headers this needs.
        """
        resp = await client.get(
            f"/users/me/messages/{message_id}",
            params={
                "format": "metadata",
                "metadataHeaders": ["Message-ID", "References", "Subject", "From"],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        headers = {
            h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])
        }
        return headers, str(data.get("threadId", ""))

    async def reply(self, message_id: str, body: str) -> str:
        token = await self._get_token()
        async with self._make_client(token) as client:
            headers, thread_id = await self._fetch_reply_headers(client, message_id)
            msg = _build_reply_mime(headers, body)
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            payload: dict[str, Any] = {"raw": raw}
            if thread_id:
                payload["threadId"] = thread_id
            resp = await client.post("/users/me/messages/send", json=payload)
            resp.raise_for_status()
            return str(resp.json()["id"])

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


def _build_reply_mime(headers: dict[str, str], body: str) -> MIMEText:
    """The outgoing MIME message for a reply to a message with the given lowercased *headers*.

    Threads via RFC-2822 headers (#461): ``In-Reply-To`` is the original's ``Message-ID``;
    ``References`` is the original's own reference chain plus that same ``Message-ID`` (RFC
    2822 recommends the full chain, not just the immediate parent), so the reply threads
    correctly even in mail clients that ignore Gmail's own ``threadId``.
    """
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = headers.get("from", "")
    msg["Subject"] = _reply_subject(headers.get("subject", ""))
    original_message_id = headers.get("message-id", "")
    if original_message_id:
        msg["In-Reply-To"] = original_message_id
    references = " ".join(filter(None, [headers.get("references", ""), original_message_id]))
    if references:
        msg["References"] = references
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
