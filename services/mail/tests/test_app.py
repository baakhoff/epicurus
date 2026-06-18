"""Tests for the mail HTTP endpoints: resolver and messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
