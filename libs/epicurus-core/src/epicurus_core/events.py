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

Tracing (#57): publish / request / handle each open an OpenTelemetry span, and the
trace context rides along in NATS message headers (W3C ``traceparent``) so a consumer
span links to the publisher's — one distributed trace across the bus. Spans carry only
the subject, tenant, and byte size; never the payload. All of this is a cheap no-op
until :func:`epicurus_core.tracing.setup_tracing` installs a provider.
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
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from epicurus_core.config import CoreSettings
from epicurus_core.logging import get_logger
from epicurus_core.tenancy import TenantError, current_tenant, scope_subject
from epicurus_core.tracing import (
    EVENT_TRACER_NAME,
    TENANT_ATTRIBUTE,
    extract_trace_context,
    get_tracer,
    inject_trace_headers,
)

__all__ = ["Event", "EventBus", "EventHandler", "Payload", "Replier"]

Payload = bytes | str | dict[str, Any]

log = get_logger("epicurus_core.events")

# No-op until a provider is installed (epicurus_core.tracing.setup_tracing), so the
# instrumentation below is always-on in code yet free when tracing is disabled.
_tracer = get_tracer(EVENT_TRACER_NAME)


def _safe_tenant(tenant_id: str | None) -> str | None:
    """The tenant to tag a span with: the explicit one, else the context's, else None
    (so a publish with no tenant still spans — the missing-tenant error surfaces when
    :func:`scope_subject` runs inside the span)."""
    if tenant_id is not None:
        return tenant_id
    try:
        return current_tenant()
    except TenantError:
        return None


def _set_msg_attrs(
    span: Span, operation: str, subject: str, tenant_id: str | None, size: int
) -> None:
    """Stamp the NATS messaging attributes on ``span`` — structure only, no payload."""
    if not span.is_recording():
        return
    span.set_attribute("messaging.system", "nats")
    span.set_attribute("messaging.operation", operation)
    span.set_attribute("messaging.destination.name", subject)
    span.set_attribute("messaging.message.body.size", size)
    tenant = _safe_tenant(tenant_id)
    if tenant is not None:
        span.set_attribute(TENANT_ATTRIBUTE, tenant)


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
        payload = _encode(data)
        with _tracer.start_as_current_span(f"{subject} publish", kind=SpanKind.PRODUCER) as span:
            _set_msg_attrs(span, "publish", subject, tenant_id, len(payload))
            scoped = scope_subject(subject, tenant_id)
            await self.client.publish(scoped, payload, headers=inject_trace_headers() or None)

    async def request(
        self,
        subject: str,
        data: Payload,
        *,
        timeout: float = 2.0,
        tenant_id: str | None = None,
    ) -> Event:
        """Request/reply: send ``data`` and await a single response."""
        payload = _encode(data)
        with _tracer.start_as_current_span(f"{subject} request", kind=SpanKind.CLIENT) as span:
            _set_msg_attrs(span, "request", subject, tenant_id, len(payload))
            scoped = scope_subject(subject, tenant_id)
            msg = await self.client.request(
                scoped, payload, timeout=timeout, headers=inject_trace_headers() or None
            )
            return Event(subject=msg.subject, data=msg.data)

    def _consumer_cb(
        self, subject: str, handler: EventHandler, tenant_id: str | None
    ) -> Callable[[Msg], Awaitable[None]]:
        """The traced, never-breaking callback every ``subscribe`` variant delivers through."""

        async def _cb(msg: Msg) -> None:
            ctx = extract_trace_context(msg.headers)
            with _tracer.start_as_current_span(
                f"{subject} process", context=ctx, kind=SpanKind.CONSUMER
            ) as span:
                _set_msg_attrs(span, "process", subject, tenant_id, len(msg.data))
                try:
                    await handler(Event(subject=msg.subject, data=msg.data))
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.record_exception(exc)
                    log.exception("event handler raised", subject=msg.subject)

        return _cb

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
        return await self.client.subscribe(
            scope_subject(subject, tenant_id),
            queue=queue,
            cb=self._consumer_cb(subject, handler, tenant_id),
        )

    async def subscribe_any_tenant(
        self,
        subject: str,
        handler: EventHandler,
        *,
        queue: str = "",
    ) -> Subscription:
        """Invoke ``handler`` for every message on ``subject`` **across all tenants**.

        Subscribes to ``*.<subject>`` — the single-token wildcard sits exactly where
        :func:`~epicurus_core.tenancy.scope_subject` puts the tenant, so this matches
        every tenant's copy of the subject and nothing else (application subjects are
        always tenant-scoped, so they always have that leading token).

        For the **core only**: on the bus the core authenticates with unrestricted pub/sub
        while a module is confined to its own tenant-scoped subjects (ADR-0066), and a
        module reaching across tenants would be a boundary violation, not a feature. The
        core needs it because a per-tenant subscription list means a tenant added at
        runtime is silently unheard until restart — an intake that quietly drops a
        tenant's events is worse than one that never had them.

        The handler must read the tenant off the message (``Event.subject``'s first token)
        or its payload, and should trust neither without checking the other agrees.
        """
        return await self.client.subscribe(
            f"*.{subject}",
            queue=queue,
            cb=self._consumer_cb(subject, handler, None),
        )

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
            ctx = extract_trace_context(msg.headers)
            with _tracer.start_as_current_span(
                f"{subject} process", context=ctx, kind=SpanKind.SERVER
            ) as span:
                _set_msg_attrs(span, "process", subject, tenant_id, len(msg.data))
                try:
                    result = await replier(Event(subject=msg.subject, data=msg.data))
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.record_exception(exc)
                    log.exception("replier raised; the request will time out", subject=msg.subject)
                    return
                if msg.reply:
                    await msg.respond(_encode(result))

        return await self.client.subscribe(scope_subject(subject, tenant_id), queue=queue, cb=_cb)
