"""NATS client — the epicurus event backbone.

Subjects are tenant-scoped via :func:`scope_subject`, so publishers and
subscribers address ``<tenant>.<base>`` without hand-building names. This talks
to NATS on the internal Docker network only — the contract is local-only.

Covers core NATS pub/sub and request/reply. JetStream persistence is a follow-up;
the infra already runs NATS with ``-js`` enabled.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from epicurus_core.config import CoreSettings
from epicurus_core.tenancy import scope_subject

__all__ = ["Event", "EventBus", "EventHandler", "Payload", "Replier"]

Payload = bytes | str | dict[str, Any]


def _encode(data: Payload) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode()
    return json.dumps(data).encode()


@dataclass(frozen=True)
class Event:
    """A received message: its fully-scoped subject and raw payload."""

    subject: str
    data: bytes

    @property
    def text(self) -> str:
        return self.data.decode()

    def json(self) -> Any:
        return json.loads(self.data)


EventHandler = Callable[[Event], Awaitable[None]]
Replier = Callable[[Event], Awaitable[Payload]]


class EventBus:
    """Async NATS client. Use as ``async with EventBus.from_settings(s) as bus``."""

    def __init__(self, url: str = "nats://localhost:4222") -> None:
        self._url = url
        self._nc: NATSClient | None = None

    @classmethod
    def from_settings(cls, settings: CoreSettings) -> EventBus:
        return cls(settings.nats_url)

    @property
    def client(self) -> NATSClient:
        if self._nc is None or not self._nc.is_connected:
            raise RuntimeError("EventBus is not connected; call connect() first")
        return self._nc

    async def connect(self) -> None:
        self._nc = await nats.connect(self._url)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None

    async def __aenter__(self) -> EventBus:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def publish(self, subject: str, data: Payload, tenant_id: str | None = None) -> None:
        """Publish ``data`` to the tenant-scoped ``subject``."""
        await self.client.publish(scope_subject(subject, tenant_id), _encode(data))

    async def request(
        self,
        subject: str,
        data: Payload,
        *,
        timeout: float = 2.0,
        tenant_id: str | None = None,
    ) -> Event:
        """Request/reply: send ``data`` and await a single response."""
        msg = await self.client.request(
            scope_subject(subject, tenant_id), _encode(data), timeout=timeout
        )
        return Event(subject=msg.subject, data=msg.data)

    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        tenant_id: str | None = None,
        queue: str = "",
    ) -> Subscription:
        """Invoke ``handler`` for every message on the tenant-scoped ``subject``."""

        async def _cb(msg: Msg) -> None:
            await handler(Event(subject=msg.subject, data=msg.data))

        return await self.client.subscribe(scope_subject(subject, tenant_id), queue=queue, cb=_cb)

    async def reply(
        self,
        subject: str,
        replier: Replier,
        *,
        tenant_id: str | None = None,
        queue: str = "",
    ) -> Subscription:
        """Serve request/reply: respond to each request with ``replier``'s result."""

        async def _cb(msg: Msg) -> None:
            result = await replier(Event(subject=msg.subject, data=msg.data))
            if msg.reply:
                await msg.respond(_encode(result))

        return await self.client.subscribe(scope_subject(subject, tenant_id), queue=queue, cb=_cb)
