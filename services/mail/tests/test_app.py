"""Tests for the mail HTTP endpoints: resolver and messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from epicurus_mail.provider import MailMessage, MailProvider


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
        sent = provider.transmit.call_args.args[0]  # type: ignore[attr-defined]
        assert (sent.to, sent.subject, sent.body) == ("bob@x.com", "Hi", "Hello")

    def test_publishes_mail_sent(self) -> None:
        provider = AsyncMock(spec=MailProvider)
        provider.transmit = AsyncMock(return_value="sent-123")
        bus = AsyncMock()
        client = self._send_client(provider, bus)
        client.post("/send", json={"to": "bob@x.com", "subject": "Hi", "body": "Hello"})
        bus.publish.assert_awaited_once()
        assert bus.publish.call_args.args[0] == "mail.sent"

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
