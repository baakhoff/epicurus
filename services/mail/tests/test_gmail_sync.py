"""Unit tests for the Gmail incremental-sync surface (ADR-0096, #623).

Covers the pure parsers (``_as_int``, ``_history_thread_ids``, ``_thread_summary``) and the
three provider methods that back the cache reconcile — ``current_cursor``,
``changed_threads_since`` (including pagination + the history-expired 404), and
``get_thread_summary`` (including the deleted-thread 404). The httpx client is mocked the same
way the rest of ``test_gmail.py`` mocks it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from epicurus_core import PlatformClient
from epicurus_mail.gmail import (
    GmailProvider,
    _as_int,
    _history_thread_ids,
    _thread_summary,
)
from epicurus_mail.provider import MailCursor


def _make_platform(access_token: str = "tok") -> PlatformClient:
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(return_value=access_token)
    return platform  # type: ignore[return-value]


def _provider() -> GmailProvider:
    return GmailProvider(platform=_make_platform(), tenant_id="local")  # type: ignore[arg-type]


def _resp(data: dict[str, Any], *, status: int = 200, error: bool = False) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=data)
    if error:
        req = httpx.Request("GET", "https://gmail.example")
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "err", request=req, response=httpx.Response(status, request=req)
            )
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _client(*responses: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(side_effect=list(responses))
    return client


def _install(provider: GmailProvider, client: AsyncMock) -> None:
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]


# ── pure parsers ──────────────────────────────────────────────────────────────


def test_as_int_coerces_gmail_string_and_tolerates_garbage() -> None:
    assert _as_int("987654") == 987654  # Gmail sends historyId as a decimal string
    assert _as_int(42) == 42
    assert _as_int(None) is None
    assert _as_int("not-a-number") is None  # defensive: never raises ValueError


def test_history_thread_ids_walks_every_change_array() -> None:
    record = {
        "messages": [{"id": "m0", "threadId": "t0"}],
        "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}],
        "messagesDeleted": [{"message": {"id": "m2", "threadId": "t2"}}],
        "labelsAdded": [{"message": {"id": "m3", "threadId": "t3"}}],
        "labelsRemoved": [{"message": {"id": "m4", "threadId": "t1"}}],  # dup thread
    }
    assert _history_thread_ids(record) == {"t0", "t1", "t2", "t3"}


def test_thread_summary_sets_sort_ts_and_label_union() -> None:
    data = {
        "id": "thread-1",
        "messages": [
            {
                "internalDate": "1000",
                "labelIds": ["INBOX", "UNREAD"],
                "payload": {"headers": [{"name": "Subject", "value": "Hi"}]},
            },
            {
                "internalDate": "1752000000000",  # newest, ~2025 epoch ms (> int32)
                "labelIds": ["INBOX", "IMPORTANT"],
                "payload": {"headers": [{"name": "From", "value": "a@x.com"}]},
            },
        ],
    }
    summary = _thread_summary(data)
    assert summary.sort_ts == 1752000000000  # newest message's internalDate
    assert summary.label_ids == ["IMPORTANT", "INBOX", "UNREAD"]  # sorted union
    assert summary.unread is True  # any message UNREAD → thread unread


# ── current_cursor ────────────────────────────────────────────────────────────


async def test_current_cursor_reads_profile_history_id() -> None:
    provider = _provider()
    _install(provider, _client(_resp({"historyId": "123456789012"})))
    cursor = await provider.current_cursor()
    assert cursor.history_id == 123456789012  # parsed to int, BigInteger-sized


# ── changed_threads_since ─────────────────────────────────────────────────────


async def test_changed_threads_since_collects_and_paginates() -> None:
    provider = _provider()
    page1 = _resp(
        {
            "history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "tA"}}]}],
            "historyId": "1001",
            "nextPageToken": "PAGE2",
        }
    )
    page2 = _resp(
        {
            "history": [{"labelsRemoved": [{"message": {"id": "m2", "threadId": "tB"}}]}],
            "historyId": "1002",
        }
    )
    _install(provider, _client(page1, page2))
    changes = await provider.changed_threads_since(MailCursor(history_id=1000))
    assert changes is not None
    assert changes.changed_thread_ids == {"tA", "tB"}
    assert changes.next_cursor.history_id == 1002  # advanced to the last page's historyId


async def test_changed_threads_since_empty_delta_advances_cursor() -> None:
    provider = _provider()
    _install(provider, _client(_resp({"historyId": "2000"})))  # no `history` key
    changes = await provider.changed_threads_since(MailCursor(history_id=1000))
    assert changes is not None
    assert changes.changed_thread_ids == set()
    assert changes.next_cursor.history_id == 2000


async def test_changed_threads_since_returns_none_on_expired_history() -> None:
    provider = _provider()
    _install(provider, _client(_resp({}, status=404)))  # startHistoryId too old
    assert await provider.changed_threads_since(MailCursor(history_id=1)) is None


async def test_changed_threads_since_cold_cursor_is_none() -> None:
    provider = _provider()
    # An empty cursor has no history_id — nothing to diff from, so signal a full sync.
    assert await provider.changed_threads_since(MailCursor()) is None


# ── get_thread_summary ────────────────────────────────────────────────────────


async def test_get_thread_summary_returns_row() -> None:
    provider = _provider()
    data = {
        "id": "t1",
        "messages": [
            {
                "internalDate": "500",
                "labelIds": ["INBOX"],
                "payload": {"headers": [{"name": "Subject", "value": "Hi"}]},
            }
        ],
    }
    _install(provider, _client(_resp(data)))
    summary = await provider.get_thread_summary("t1")
    assert summary is not None
    assert summary.id == "t1"
    assert summary.label_ids == ["INBOX"]


async def test_get_thread_summary_none_when_deleted() -> None:
    provider = _provider()
    _install(provider, _client(_resp({}, status=404, error=True)))  # thread 404s
    assert await provider.get_thread_summary("gone") is None


async def test_get_thread_summary_reraises_other_errors() -> None:
    provider = _provider()
    _install(provider, _client(_resp({}, status=500, error=True)))
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_thread_summary("t1")
