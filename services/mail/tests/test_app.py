"""Tests for the mail HTTP endpoints: resolver, messages, and the mailbox page (ADR-0087)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import EventEnvelope
from epicurus_mail.provider import (
    AttachmentContent,
    ComposedMessage,
    MailCategory,
    MailCategoryPreview,
    MailCursor,
    MailLabel,
    MailMessage,
    MailNotConnected,
    MailProvider,
    MailThread,
    MailThreadSummary,
    ThreadPage,
)


def _test_engine() -> AsyncEngine:
    """A fresh in-memory SQLite cache engine per app, isolated across tests (ADR-0096)."""
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def _categories_off(provider: MailProvider) -> None:
    """Default a provider stub to "no inbox categories" unless the test says otherwise (#765).

    ``list_categories`` / ``category_query`` are concrete no-ops on the seam — a provider that
    doesn't classify mail inherits them — but ``AsyncMock(spec=MailProvider)`` still auto-stubs
    them into truthy mocks. Stating the capability-gated default here keeps every pre-tabs test
    asserting exactly the page it was written for.
    """
    if not hasattr(provider.list_categories, "assert_awaited"):
        provider.list_categories = AsyncMock(return_value=[])  # type: ignore[method-assign]
        provider.category_query = MagicMock(return_value=None)  # type: ignore[method-assign]


def _client_with_provider(provider: MailProvider) -> TestClient:
    """A TestClient over *provider* with a mocked event bus + in-memory cache (ADR-0087 tests).

    The cache path (the plain landing view) needs the schema, which ``create_app`` builds in
    its lifespan — enter the client as a context manager (``with``) for those tests so
    ``MailCache.init`` runs; the live/thread/send routes work without it.

    The background reconcile is switched **off** here (``MAIL_POLL_INTERVAL_S=0``, #796): these
    tests assert exact provider call counts for one page read, and a poll tick racing the
    request would reconcile the same mailbox underneath them. That the lifespan really does
    start the loop when it is configured is asserted in ``test_mail_poller.py``.
    """
    if not hasattr(provider.health_check, "assert_awaited"):  # ensure health_check is mocked
        provider.health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]
    _categories_off(provider)
    with (
        patch.dict(os.environ, {"MAIL_POLL_INTERVAL_S": "0"}),
        patch("epicurus_mail.app.GmailProvider", return_value=provider),
        patch("epicurus_mail.app.EventBus.from_settings", return_value=AsyncMock()),
    ):
        from epicurus_mail.app import create_app

        app = create_app(engine=_test_engine())
    return TestClient(app, raise_server_exceptions=True)


def _sample() -> MailMessage:
    return MailMessage(
        id="msg1",
        thread_id="thread1",
        subject="Invoice from Acme",
        sender="acme@example.com",
        to=["me@example.com"],
        date="Mon, 1 Jan 2024 10:00:00 +0000",
        snippet="Please find attached…",
        body="Dear customer,\n\nPlease find the invoice attached.\n\nRegards",
    )


def _client_with_message(message: MailMessage) -> TestClient:
    """A TestClient whose provider returns *message* from ``read`` (mocked bus)."""
    provider = AsyncMock(spec=MailProvider)
    provider.read = AsyncMock(return_value=message)
    provider.health_check = AsyncMock(return_value=True)
    with (
        patch("epicurus_mail.app.GmailProvider", return_value=provider),
        patch("epicurus_mail.app.EventBus.from_settings", return_value=AsyncMock()),
    ):
        from epicurus_mail.app import create_app

        app = create_app()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def client() -> TestClient:
    """TestClient with a mocked provider and a disabled event bus."""
    provider = AsyncMock(spec=MailProvider)
    provider.read = AsyncMock(return_value=_sample())
    provider.health_check = AsyncMock(return_value=True)

    with (
        patch("epicurus_mail.app.GmailProvider", return_value=provider),
        patch("epicurus_mail.app.EventBus.from_settings") as mock_bus_factory,
    ):
        mock_bus = AsyncMock()
        mock_bus_factory.return_value = mock_bus
        from epicurus_mail.app import create_app

        app = create_app()
    return TestClient(app, raise_server_exceptions=True)


class TestResolveMessage:
    def test_returns_hovercard_shape(self, client: TestClient) -> None:
        resp = client.get("/resolve/message/msg1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Invoice from Acme"
        assert body["description"] == "Please find attached…"
        details = {d["label"]: d["value"] for d in body["details"]}
        assert details["From"] == "acme@example.com"
        assert details["To"] == "me@example.com"
        assert details["Date"] == "Mon, 1 Jan 2024 10:00:00 +0000"

    def test_no_href_field(self, client: TestClient) -> None:
        resp = client.get("/resolve/message/msg1")
        body = resp.json()
        assert "href" not in body or body.get("href") is None

    def test_leads_with_unread_status_when_unread(self) -> None:
        msg = _sample()
        msg.unread = True
        local_client = _client_with_message(msg)
        body = local_client.get("/resolve/message/msg1").json()
        # The unread flag leads the detail rows so it is immediately visible.
        assert body["details"][0] == {"label": "Status", "value": "Unread"}

    def test_omits_status_row_when_read(self, client: TestClient) -> None:
        # The default fixture sample is read (unread defaults False) → no Status row.
        body = client.get("/resolve/message/msg1").json()
        assert "Status" not in {d["label"] for d in body["details"]}

    def test_missing_message_returns_404(self, client: TestClient) -> None:
        with patch("epicurus_mail.app.GmailProvider") as mock_cls:
            provider = AsyncMock(spec=MailProvider)
            provider.read = AsyncMock(side_effect=Exception("not found"))
            provider.health_check = AsyncMock(return_value=True)
            mock_cls.return_value = provider
            with patch("epicurus_mail.app.EventBus.from_settings") as mock_bus_factory:
                mock_bus = AsyncMock()
                mock_bus_factory.return_value = mock_bus
                from epicurus_mail.app import create_app

                app = create_app()
            local_client = TestClient(app, raise_server_exceptions=False)
            resp = local_client.get("/resolve/message/bad-id")
            assert resp.status_code == 404


class TestGetMessage:
    def test_returns_email_message_shape(self, client: TestClient) -> None:
        resp = client.get("/messages/msg1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["subject"] == "Invoice from Acme"
        assert body["from"] == "acme@example.com"
        assert body["date"] == "Mon, 1 Jan 2024 10:00:00 +0000"
        assert "Dear customer" in body["body"]

    def test_body_is_full_not_snippet(self, client: TestClient) -> None:
        resp = client.get("/messages/msg1")
        body = resp.json()
        assert "invoice attached" in body["body"]

    def test_includes_module_id_and_read_state(self, client: TestClient) -> None:
        # The reader needs module + id to invoke actions and re-fetch itself (#277).
        body = client.get("/messages/msg1").json()
        assert body["module"] == "mail"
        assert body["message_id"] == "msg1"
        assert body["unread"] is False

    def test_read_message_offers_mark_unread_action(self, client: TestClient) -> None:
        # The default sample is read → the toggle marks it unread.
        body = client.get("/messages/msg1").json()
        assert len(body["actions"]) == 1
        action = body["actions"][0]
        assert action["tool"] == "mail_mark_unread"
        assert action["label"] == "Mark as unread"
        assert action["args"] == {"message_id": "msg1"}

    def test_unread_message_offers_mark_read_action(self) -> None:
        msg = _sample()
        msg.unread = True
        local_client = _client_with_message(msg)
        body = local_client.get("/messages/msg1").json()
        assert body["unread"] is True
        action = body["actions"][0]
        assert action["tool"] == "mail_mark_read"
        assert action["label"] == "Mark as read"
        assert action["args"] == {"message_id": "msg1"}

    def test_missing_message_returns_404(self, client: TestClient) -> None:
        with patch("epicurus_mail.app.GmailProvider") as mock_cls:
            provider = AsyncMock(spec=MailProvider)
            provider.read = AsyncMock(side_effect=Exception("not found"))
            provider.health_check = AsyncMock(return_value=True)
            mock_cls.return_value = provider
            with patch("epicurus_mail.app.EventBus.from_settings") as mock_bus_factory:
                mock_bus = AsyncMock()
                mock_bus_factory.return_value = mock_bus
                from epicurus_mail.app import create_app

                app = create_app()
            local_client = TestClient(app, raise_server_exceptions=False)
            resp = local_client.get("/messages/bad-id")
            assert resp.status_code == 404

    def test_no_body_returns_empty_string(self, client: TestClient) -> None:
        with patch("epicurus_mail.app.GmailProvider") as mock_cls:
            provider = AsyncMock(spec=MailProvider)
            msg = _sample()
            msg.body = None
            provider.read = AsyncMock(return_value=msg)
            provider.health_check = AsyncMock(return_value=True)
            mock_cls.return_value = provider
            with patch("epicurus_mail.app.EventBus.from_settings") as mock_bus_factory:
                mock_bus = AsyncMock()
                mock_bus_factory.return_value = mock_bus
                from epicurus_mail.app import create_app

                app = create_app()
            local_client = TestClient(app, raise_server_exceptions=True)
            resp = local_client.get("/messages/msg1")
            assert resp.status_code == 200
            assert resp.json()["body"] == ""


class TestStatus:
    """/status reports connection from a fast token-presence check, not a live call (#209)."""

    def _status_client(self, *, available: bool) -> TestClient:
        provider = AsyncMock(spec=MailProvider)
        provider.is_available = AsyncMock(return_value=available)
        with (
            patch("epicurus_mail.app.GmailProvider", return_value=provider),
            patch("epicurus_mail.app.EventBus.from_settings", return_value=AsyncMock()),
        ):
            from epicurus_mail.app import create_app

            app = create_app()
        return TestClient(app, raise_server_exceptions=True)

    def test_reports_connected(self) -> None:
        resp = self._status_client(available=True).get("/status")
        assert resp.status_code == 200
        assert resp.json() == {"gmail_connected": True}

    def test_reports_disconnected(self) -> None:
        resp = self._status_client(available=False).get("/status")
        assert resp.status_code == 200
        assert resp.json() == {"gmail_connected": False}


class TestSend:
    """POST /send transmits an operator-confirmed draft — the module's only send path (ADR-0085)."""

    def _send_client(self, provider: MailProvider, bus: AsyncMock) -> TestClient:
        with (
            patch("epicurus_mail.app.GmailProvider", return_value=provider),
            patch("epicurus_mail.app.EventBus.from_settings", return_value=bus),
        ):
            from epicurus_mail.app import create_app

            app = create_app()
        return TestClient(app, raise_server_exceptions=False)

    def test_transmits_the_reviewed_draft_and_returns_id(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.transmit = AsyncMock(return_value="sent-123")
        client = self._send_client(provider, AsyncMock())
        resp = client.post("/send", json={"to": "bob@x.com", "subject": "Hi", "body": "Hello"})
        assert resp.status_code == 200
        assert resp.json() == {"id": "sent-123"}
        # Transmitted byte-identical to what was reviewed (ADR-0085).
        sent = provider.transmit.call_args.args[0]
        assert (sent.to, sent.subject, sent.body) == ("bob@x.com", "Hi", "Hello")

    def test_publishes_mail_sent(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.transmit = AsyncMock(return_value="sent-123")
        bus = AsyncMock()
        client = self._send_client(provider, bus)
        client.post("/send", json={"to": "bob@x.com", "subject": "Hi", "body": "Hello"})
        bus.publish.assert_awaited_once()
        # mail.sent now rides the module event spine (#663): the wire subject gains the
        # events. prefix, and the payload is a validated EventEnvelope, not an ad-hoc dict.
        subject, data, tenant_id = (
            bus.publish.call_args.args[0],
            bus.publish.call_args.args[1],
            (bus.publish.call_args.kwargs.get("tenant_id")),
        )
        assert subject == "events.mail.sent"
        envelope = EventEnvelope.model_validate(data)
        assert envelope.type == "mail.sent"
        assert envelope.module == "mail"
        assert envelope.dedup_key == "sent-123"
        assert envelope.payload == {"to": "bob@x.com", "subject": "Hi"}
        assert tenant_id is not None

    def test_403_returns_reconnect_hint(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.transmit = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "403 Forbidden",
                request=httpx.Request("POST", "http://gmail/send"),
                response=httpx.Response(403),
            )
        )
        client = self._send_client(provider, AsyncMock())
        resp = client.post("/send", json={"to": "bob@x.com", "subject": "Hi", "body": "Hello"})
        assert resp.status_code == 403
        assert "Reconnect Google" in resp.json()["detail"]

    def test_429_returns_rate_limit_hint_under_429(self) -> None:
        # A Gmail 429 on transmit surfaces the wait-and-retry hint under a 429, not a raw 500,
        # honoring Retry-After when Gmail sends it (#557).
        provider = AsyncMock(spec=MailProvider)
        provider.transmit = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=httpx.Request("POST", "http://gmail/send"),
                response=httpx.Response(429, headers={"Retry-After": "12"}),
            )
        )
        client = self._send_client(provider, AsyncMock())
        resp = client.post("/send", json={"to": "bob@x.com", "subject": "Hi", "body": "Hello"})
        assert resp.status_code == 429
        detail = resp.json()["detail"]
        assert "rate-limiting" in detail
        assert "12 seconds" in detail  # Retry-After surfaced

    def test_a_bus_failure_does_not_fail_a_completed_send(self) -> None:
        # The mail already went out; a bus hiccup must not turn a success into a 500.
        provider = AsyncMock(spec=MailProvider)
        provider.transmit = AsyncMock(return_value="sent-123")
        bus = AsyncMock()
        bus.publish = AsyncMock(side_effect=Exception("bus down"))
        client = self._send_client(provider, bus)
        resp = client.post("/send", json={"to": "b@x.com", "subject": "s", "body": "b"})
        assert resp.status_code == 200
        assert resp.json() == {"id": "sent-123"}


# ── mailbox page routes (ADR-0087) ───────────────────────────────────────────


def _mailbox_message(msg_id: str = "m1", *, body: str = "Hello") -> MailMessage:
    return MailMessage(
        id=msg_id,
        thread_id="t1",
        subject="Hi",
        sender="a@x.com",
        to=["me@x.com"],
        date="",
        snippet="",
        body=body,
    )


class TestMailboxListRoute:
    def test_returns_rail_and_threads(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.current_cursor = AsyncMock(return_value=MailCursor(history_id=1))
        provider.list_labels = AsyncMock(
            return_value=[MailLabel(id="INBOX", title="Inbox", unread=2)]
        )
        provider.list_threads = AsyncMock(
            return_value=ThreadPage(
                threads=[
                    MailThreadSummary(id="t1", subject="Hi", sender="a@x.com", snippet="…", date="")
                ],
                next_cursor="N2",
            )
        )
        # Landing goes through the cache (ADR-0096): enter the client so MailCache.init runs;
        # a cold cache does a one-time full sync from the (mocked) provider.
        with _client_with_provider(provider) as client:
            resp = client.get("/pages/mailbox")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_label"] == "INBOX"
        assert body["labels"][0]["unread"] == 2
        assert body["threads"][0]["id"] == "t1"
        assert body["next_cursor"] == "N2"

    def test_forwards_label_query_cursor(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.list_labels = AsyncMock(return_value=[])
        provider.list_threads = AsyncMock(return_value=ThreadPage(threads=[]))
        _client_with_provider(provider).get("/pages/mailbox?label=SENT&cursor=C1")
        provider.list_threads.assert_awaited_once_with(
            label="SENT", query=None, cursor="C1", limit=25
        )

    def test_tabs_are_absent_when_the_provider_has_no_categories(self) -> None:
        """The capability gate end-to-end: today's payload, with an empty tabs block (#765)."""
        provider = AsyncMock(spec=MailProvider)
        provider.current_cursor = AsyncMock(return_value=MailCursor(history_id=1))
        provider.list_labels = AsyncMock(return_value=[MailLabel(id="INBOX", title="Inbox")])
        provider.list_threads = AsyncMock(return_value=ThreadPage(threads=[]))
        with _client_with_provider(provider) as client:
            body = client.get("/pages/mailbox").json()
        assert body["tabs"] == []
        assert body["active_tab"] == ""

    def test_inbox_returns_the_tab_strip(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.current_cursor = AsyncMock(return_value=MailCursor(history_id=1))
        provider.list_labels = AsyncMock(return_value=[MailLabel(id="INBOX", title="Inbox")])
        provider.list_threads = AsyncMock(return_value=ThreadPage(threads=[]))
        provider.list_categories = AsyncMock(
            return_value=[
                MailCategory(id="primary", title="Primary", unread=2),
                MailCategory(
                    id="promotions",
                    title="Promotions",
                    unread=41,
                    preview=MailCategoryPreview(sender="deals@x.com", subject="50% off"),
                ),
            ]
        )
        provider.category_query = MagicMock(side_effect=lambda cid: f"category:{cid}")
        with _client_with_provider(provider) as client:
            body = client.get("/pages/mailbox").json()
        assert [t["id"] for t in body["tabs"]] == ["primary", "promotions"]
        assert body["tabs"][1]["unread"] == 41
        # The wire key is ``from``, not the model's ``sender``.
        assert body["tabs"][1]["preview"] == {"from": "deals@x.com", "subject": "50% off"}

    def test_tab_param_scopes_the_thread_list(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.list_labels = AsyncMock(return_value=[])
        provider.list_threads = AsyncMock(return_value=ThreadPage(threads=[]))
        provider.list_categories = AsyncMock(
            return_value=[MailCategory(id="promotions", title="Promotions")]
        )
        provider.category_query = MagicMock(side_effect=lambda cid: f"category:{cid}")
        with _client_with_provider(provider) as client:
            body = client.get("/pages/mailbox?tab=promotions").json()
        provider.list_threads.assert_awaited_once_with(
            label="INBOX", query="category:promotions", cursor=None, limit=25
        )
        assert body["active_tab"] == "promotions"

    def test_thread_id_returns_conversation(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.get_thread = AsyncMock(
            return_value=MailThread(id="t1", subject="Hi", messages=[_mailbox_message("m1")])
        )
        provider.compose_reply = AsyncMock(
            return_value=ComposedMessage(to="a@x.com", subject="Re: Hi", body="")
        )
        resp = _client_with_provider(provider).get("/pages/mailbox?thread_id=t1")
        assert resp.status_code == 200
        thread = resp.json()["thread"]
        assert thread["id"] == "t1"
        assert thread["messages"][0]["message_id"] == "m1"
        assert thread["reply"]["reply_to_message_id"] == "m1"

    def test_unknown_page_is_404(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        resp = _client_with_provider(provider).get("/pages/nope")
        assert resp.status_code == 404

    def test_gmail_scope_error_relayed_with_status(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        request = httpx.Request("GET", "https://gmail.googleapis.com")
        response = httpx.Response(403, request=request, json={"error": {"message": "no scope"}})
        provider.list_labels = AsyncMock(
            side_effect=httpx.HTTPStatusError("403", request=request, response=response)
        )
        # The cold-cache full sync calls list_labels, which raises here; the route relays the
        # Gmail scope hint with its status (the cache never masks a provider error).
        with _client_with_provider(provider) as client:
            resp = client.get("/pages/mailbox")
        assert resp.status_code == 403
        assert "Reconnect Google" in resp.json()["detail"]


class TestMailboxSendRoute:
    def test_compose_transmits_a_fresh_message(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.transmit = AsyncMock(return_value="sent-1")
        resp = _client_with_provider(provider).post(
            "/pages/mailbox/send", json={"to": "b@x.com", "subject": "Hi", "body": "Body"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"id": "sent-1"}
        sent = provider.transmit.await_args.args[0]
        assert isinstance(sent, ComposedMessage)
        assert sent.to == "b@x.com" and sent.subject == "Hi" and sent.body == "Body"
        provider.compose_reply.assert_not_awaited()

    def test_reply_rederives_threading_server_side(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.compose_reply = AsyncMock(
            return_value=ComposedMessage(
                to="a@x.com", subject="Re: Hi", body="My reply", thread_id="t1"
            )
        )
        provider.transmit = AsyncMock(return_value="sent-2")
        resp = _client_with_provider(provider).post(
            "/pages/mailbox/send", json={"reply_to_message_id": "m9", "body": "My reply"}
        )
        assert resp.status_code == 200
        # The module (not the web) derives threading from the original message id (#461).
        provider.compose_reply.assert_awaited_once_with("m9", "My reply")
        assert provider.transmit.await_args.args[0].thread_id == "t1"

    def test_compose_without_recipient_is_400(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        resp = _client_with_provider(provider).post("/pages/mailbox/send", json={"body": "Body"})
        assert resp.status_code == 400
        provider.transmit.assert_not_awaited()

    def test_send_to_unknown_page_is_404(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        resp = _client_with_provider(provider).post(
            "/pages/nope/send", json={"body": "x", "to": "a@x.com"}
        )
        assert resp.status_code == 404


class TestMailboxMarkReadRoute:
    """Mark-thread-read-on-open (#625): provider marks + local-cache write-through."""

    @staticmethod
    def _provider_with_unread_thread() -> AsyncMock:
        provider = AsyncMock(spec=MailProvider)
        provider.current_cursor = AsyncMock(return_value=MailCursor(history_id=1))
        provider.list_labels = AsyncMock(
            return_value=[MailLabel(id="INBOX", title="Inbox", unread=1)]
        )
        provider.list_threads = AsyncMock(
            return_value=ThreadPage(
                threads=[
                    MailThreadSummary(
                        id="t1", subject="Hi", sender="a@x.com", snippet="", date="", unread=True
                    )
                ],
                next_cursor=None,
            )
        )
        provider.set_unread = AsyncMock(return_value=None)
        return provider

    def test_marks_provider_and_converges_cache(self) -> None:
        provider = self._provider_with_unread_thread()
        with _client_with_provider(provider) as client:
            # Cold landing populates the cache with an unread thread row.
            assert client.get("/pages/mailbox").json()["threads"][0]["unread"] is True
            resp = client.post(
                "/pages/mailbox/mark-read",
                json={"thread_id": "t1", "message_ids": ["m1", "m2"]},
            )
            assert resp.status_code == 200
            assert resp.json() == {"thread_id": "t1", "marked": 2}
            # The landing now serves the row read from cache — no reconcile needed.
            assert client.get("/pages/mailbox").json()["threads"][0]["unread"] is False
        # Each message was cleared at the provider (the #277 seam).
        assert provider.set_unread.await_count == 2
        provider.set_unread.assert_any_await("m1", unread=False)
        provider.set_unread.assert_any_await("m2", unread=False)

    def test_empty_message_ids_is_a_noop(self) -> None:
        provider = self._provider_with_unread_thread()
        with _client_with_provider(provider) as client:
            resp = client.post(
                "/pages/mailbox/mark-read", json={"thread_id": "t1", "message_ids": []}
            )
        assert resp.status_code == 200
        assert resp.json()["marked"] == 0
        provider.set_unread.assert_not_awaited()

    def test_relays_gmail_scope_error(self) -> None:
        provider = self._provider_with_unread_thread()
        request = httpx.Request("POST", "https://gmail.googleapis.com")
        response = httpx.Response(403, request=request, json={"error": {"message": "no scope"}})
        provider.set_unread = AsyncMock(
            side_effect=httpx.HTTPStatusError("403", request=request, response=response)
        )
        with _client_with_provider(provider) as client:
            resp = client.post(
                "/pages/mailbox/mark-read", json={"thread_id": "t1", "message_ids": ["m1"]}
            )
        assert resp.status_code == 403
        assert "Reconnect Google" in resp.json()["detail"]

    def test_unknown_page_is_404(self) -> None:
        provider = self._provider_with_unread_thread()
        resp = _client_with_provider(provider).post(
            "/pages/nope/mark-read", json={"thread_id": "t1", "message_ids": ["m1"]}
        )
        assert resp.status_code == 404


class TestMailboxAttachmentRoute:
    def test_streams_bytes_with_disposition(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.get_attachment = AsyncMock(
            return_value=AttachmentContent(
                filename="report.pdf", mime_type="application/pdf", content=b"PDFDATA"
            )
        )
        resp = _client_with_provider(provider).get(
            "/pages/mailbox/attachment?message_id=m1&attachment_id=att1"
        )
        assert resp.status_code == 200
        assert resp.content == b"PDFDATA"
        assert resp.headers["content-type"] == "application/pdf"
        assert 'filename="report.pdf"' in resp.headers["content-disposition"]
        provider.get_attachment.assert_awaited_once_with("m1", "att1")

    def test_missing_attachment_is_404(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        request = httpx.Request("GET", "https://gmail.googleapis.com")
        response = httpx.Response(404, request=request)
        provider.get_attachment = AsyncMock(
            side_effect=httpx.HTTPStatusError("404", request=request, response=response)
        )
        resp = _client_with_provider(provider).get(
            "/pages/mailbox/attachment?message_id=m1&attachment_id=missing"
        )
        assert resp.status_code == 404

    def test_disposition_strips_unsafe_filename_chars(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.get_attachment = AsyncMock(
            return_value=AttachmentContent(
                filename='ev"il\r\n.pdf', mime_type="application/octet-stream", content=b"x"
            )
        )
        resp = _client_with_provider(provider).get(
            "/pages/mailbox/attachment?message_id=m1&attachment_id=a1"
        )
        disposition = resp.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert 'filename="evil.pdf"' in disposition


class TestGoogleNotConnected:
    """Every HTTP surface when no Google account is connected (#764).

    Mail has no local provider to fall back to (ADR-0032), so this is not an error state to
    be swallowed — it is a state to be *named*. The list read answers honestly and 200s (plain
    navigation to Mail must never error); everything that genuinely cannot proceed answers 503
    with the one reconnect sentence, never a raw traceback and never a misleading 404.
    """

    NOT_CONNECTED = 503

    def _provider(self) -> MailProvider:
        provider = AsyncMock(spec=MailProvider)
        for method in ("read", "get_thread", "transmit", "set_unread", "get_attachment"):
            setattr(provider, method, AsyncMock(side_effect=MailNotConnected("not connected")))
        provider.compose_reply = AsyncMock(side_effect=MailNotConnected("not connected"))
        provider.is_available = AsyncMock(return_value=False)
        return provider

    def _assert_reconnect_hint(self, resp: httpx2.Response) -> None:
        assert resp.status_code == self.NOT_CONNECTED
        detail = resp.json()["detail"]
        assert "not connected" in detail and "Settings" in detail

    def test_status_reports_disconnected(self) -> None:
        resp = _client_with_provider(self._provider()).get("/status")
        assert resp.status_code == 200
        assert resp.json() == {"gmail_connected": False}

    def test_the_page_is_an_honest_empty_state_not_an_error(self) -> None:
        with _client_with_provider(self._provider()) as client:
            resp = client.get("/pages/mailbox")
        assert resp.status_code == 200
        body = resp.json()
        assert body["disconnected"] is True
        assert body["threads"] == []
        assert body["labels"] == []

    def test_the_page_never_serves_cached_mail_after_a_disconnect(self) -> None:
        """The regression the flag exists for: a warm cache would render mail we can't refresh."""
        provider = self._provider()
        with _client_with_provider(provider) as client:
            client.get("/pages/mailbox")
            resp = client.get("/pages/mailbox?reconcile=1")
        assert resp.json()["disconnected"] is True
        # Not one provider call: the gate runs before both the cached and the live read path.
        provider.list_threads.assert_not_awaited()  # type: ignore[attr-defined]
        provider.changed_threads_since.assert_not_awaited()  # type: ignore[attr-defined]

    def test_opening_a_thread_is_503_with_the_hint(self) -> None:
        client = TestClient(
            _client_with_provider(self._provider()).app, raise_server_exceptions=False
        )
        self._assert_reconnect_hint(client.get("/pages/mailbox?thread_id=t1"))

    def test_reading_a_message_is_503_not_a_misleading_404(self) -> None:
        """A leftover chip must not claim the message was deleted (#764)."""
        client = TestClient(
            _client_with_provider(self._provider()).app, raise_server_exceptions=False
        )
        self._assert_reconnect_hint(client.get("/messages/msg1"))

    def test_resolving_a_hover_card_is_503_not_a_misleading_404(self) -> None:
        client = TestClient(
            _client_with_provider(self._provider()).app, raise_server_exceptions=False
        )
        self._assert_reconnect_hint(client.get("/resolve/message/msg1"))

    def test_confirming_a_draft_is_503_with_the_hint(self) -> None:
        client = TestClient(
            _client_with_provider(self._provider()).app, raise_server_exceptions=False
        )
        self._assert_reconnect_hint(
            client.post("/send", json={"to": "b@x.com", "subject": "s", "body": "b"})
        )

    def test_page_send_is_503_with_the_hint(self) -> None:
        client = TestClient(
            _client_with_provider(self._provider()).app, raise_server_exceptions=False
        )
        self._assert_reconnect_hint(
            client.post("/pages/mailbox/send", json={"to": "b@x.com", "subject": "s", "body": "b"})
        )

    def test_mark_read_is_503_with_the_hint(self) -> None:
        client = TestClient(
            _client_with_provider(self._provider()).app, raise_server_exceptions=False
        )
        self._assert_reconnect_hint(
            client.post("/pages/mailbox/mark-read", json={"thread_id": "t1", "message_ids": ["m1"]})
        )

    def test_attachment_download_is_503_not_a_misleading_404(self) -> None:
        client = TestClient(
            _client_with_provider(self._provider()).app, raise_server_exceptions=False
        )
        self._assert_reconnect_hint(
            client.get("/pages/mailbox/attachment?message_id=m1&attachment_id=a1")
        )
