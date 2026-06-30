"""Unit tests for EventBus credential plumbing (ADR-0066) — no Docker required.

The end-to-end auth gate (anonymous rejected, credentialed accepted) is exercised
against a real authenticated NATS container in ``test_events.py``; these fast tests
pin down that the configured ``user``/``password`` actually reach ``nats.connect`` and
that the default is anonymous.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from epicurus_core.config import CoreSettings
from epicurus_core.events import EventBus


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """Replace ``nats.connect`` with an AsyncMock returning a connected fake client."""
    client = MagicMock()
    client.is_connected = True
    client.drain = AsyncMock()
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr("epicurus_core.events.nats.connect", connect)
    yield connect


async def test_connect_forwards_credentials(fake_connect: AsyncMock) -> None:
    async with EventBus("nats://nats:4222", user="core", password="s3cret"):
        pass
    kwargs = fake_connect.await_args.kwargs
    assert kwargs["user"] == "core"
    assert kwargs["password"] == "s3cret"


async def test_connect_is_anonymous_by_default(fake_connect: AsyncMock) -> None:
    async with EventBus("nats://nats:4222"):
        pass
    kwargs = fake_connect.await_args.kwargs
    assert kwargs["user"] is None
    assert kwargs["password"] is None


async def test_from_settings_uses_configured_credentials(fake_connect: AsyncMock) -> None:
    settings = CoreSettings(nats_url="nats://nats:4222", nats_user="module", nats_password="pw")
    async with EventBus.from_settings(settings):
        pass
    kwargs = fake_connect.await_args.kwargs
    assert (kwargs["user"], kwargs["password"]) == ("module", "pw")
