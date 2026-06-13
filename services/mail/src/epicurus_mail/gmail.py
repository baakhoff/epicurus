"""GmailProvider — Gmail API v1 backed by the platform OAuth token.

The provider fetches a valid access token via ``PlatformClient.oauth_token``
(which calls ``GET /platform/v1/oauth/google/token`` on the core).  It never
holds a client secret or refresh token.

Required Google OAuth scopes (requested when the operator connects via
``GET /platform/v1/oauth/google/connect?scope=...``):
    https://www.googleapis.com/auth/gmail.readonly
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

# Scopes the operator must grant when connecting Google for this module.
GMAIL_REQUIRED_SCOPE = (
    "openid email profile"
    " https://www.googleapis.com/auth/gmail.readonly"
    " https://www.googleapis.com/auth/gmail.send"
)


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
        resp = await self._platform.oauth_token("google")
        return resp.access_token

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

    async def health_check(self) -> bool:
        try:
            token = await self._get_token()
            async with self._make_client(token) as client:
                resp = await client.get("/users/me/profile")
                return resp.status_code == 200
        except Exception:
            return False


def _parse_message(data: dict[str, Any], *, full: bool) -> MailMessage:
    """Convert a Gmail API message object to a ``MailMessage``."""
    headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
    to_raw = headers.get("to", "")
    to_list = [t.strip() for t in to_raw.split(",") if t.strip()]
    body: str | None = None
    if full:
        body = _extract_body(data.get("payload", {}))
    return MailMessage(
        id=data["id"],
        thread_id=data.get("threadId", ""),
        subject=headers.get("subject", "(no subject)"),
        sender=headers.get("from", ""),
        to=to_list,
        date=headers.get("date", ""),
        snippet=data.get("snippet", ""),
        body=body,
    )


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
