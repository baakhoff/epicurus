"""NATS client — the epicurus event backbone.

Subjects are tenant-scoped via :func:`scope_subject`, so publishers and
subscribers address ``<tenant>.<base>`` without hand-building names. This talks
to NATS on the internal Docker network only — the contract is local-only.

Covers core NATS pub/sub and request/reply. JetStream persistence is a follow-up;
the infra already runs NATS with ``-js`` enabled.

Failure behavior: a subscriber handler or replier that raises is logged (with
traceback) and does not break the subscription — later messages are still
delivered. A raising replier sends no response, so the requester times out;
the failure is visible in the *replier's* logs. Connection drops, reconnects,
and client errors are logged too.
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
from epicurus_core.logging import get_logger
from epicurus_core.tenancy import scope_subject

__all__ = ["Event", "EventBus", "EventHandler", "Payload", "Replier"]

Payload = bytes | str | dict[str, Any]

log = get_logger("epicurus_core.events")


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
    """Async NATS client. Use as ``async with EventBus.from_settings(s) as bus``.

    ``user``/``password`` authenticate the connection (ADR-0066). They are optional:
    when both are ``None`` the client connects anonymously, which keeps the bus usable
    against an un-authenticated server (e.g. the integration testcontainers).
    """

    def __init__(
        self,
        url: str = "nats://localhost:4222",
        *,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._url = url
        self._user = user
        self._password = password
        self._nc: NATSClient | None = None

    @classmethod
    def from_settings(cls, settings: CoreSettings) -> EventBus:
        return cls(settings.nats_url, user=settings.nats_user, password=settings.nats_password)

    @property
    def client(self) -> NATSClient:
        if self._nc is None or not self._nc.is_connected:
            raise RuntimeError("EventBus is not connected; call connect() first")
        return self._nc

    async def connect(self) -> None:
        # user/password are forwarded only when set; nats-py treats ``None`` as
        # "no credentials" (anonymous), so an un-authenticated server still works.
        self._nc = await nats.connect(
            self._url,
            user=self._user,
            password=self._password,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnected,
            reconnected_cb=self._on_reconnected,
        )

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None

    async def _on_error(self, exc: Exception) -> None:
        log.error("nats client error", url=self._url, error=str(exc))

    async def _on_disconnected(self) -> None:
        log.warning("nats disconnected", url=self._url)

    async def _on_reconnected(self) -> None:
        log.info("nats reconnected", url=self._url)

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
        """Invoke ``handler`` for every message on the tenant-scoped ``subject``.

        A raising handler is logged and skipped; the subscription keeps
        delivering subsequent messages.
        """

        async def _cb(msg: Msg) -> None:
            try:
                await handler(Event(subject=msg.subject, data=msg.data))
            except Exception:
                log.exception("event handler raised", subject=msg.subject)

        return await self.client.subscribe(scope_subject(subject, tenant_id), queue=queue, cb=_cb)

    async def reply(
        self,
        subject: str,
        replier: Replier,
        *,
        tenant_id: str | None = None,
        queue: str = "",
    ) -> Subscription:
        """Serve request/reply: respond to each request with ``replier``'s result.

        A raising replier is logged and sends no response — the requester times
        out — and the subscription keeps serving subsequent requests.
        """

        async def _cb(msg: Msg) -> None:
            try:
                result = await replier(Event(subject=msg.subject, data=msg.data))
            except Exception:
                log.exception("replier raised; the request will time out", subject=msg.subject)
                return
            if msg.reply:
                await msg.respond(_encode(result))

        return await self.client.subscribe(scope_subject(subject, tenant_id), queue=queue, cb=_cb)
