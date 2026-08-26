"""NATS client — the epicurus event backbone.

Subjects are tenant-scoped via :func:`scope_subject`, so publishers and
subscribers address ``<tenant>.<base>`` without hand-building names. This talks
to NATS on the internal Docker network only — the contract is local-only.

Covers core NATS pub/sub, request/reply, and the two JetStream primitives a durable
consumer needs: :meth:`EventBus.ensure_stream` and
:meth:`EventBus.pull_subscribe_any_tenant`. **Publishing stays core pub/sub on every
path.** A JetStream stream captures whatever lands on its subjects, whoever published
it, so persistence is a property of the *subject* rather than of the publisher's API —
which is what lets the module event spine become at-least-once without a single
emitter changing (ADR-0103 §4, as amended). The honest cost is that
:meth:`publish` returns when the local client has accepted the message, not when the
server has stored it; see :func:`epicurus_core.module_events.emit_event` for the
resulting failure window.

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
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import APIError
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

    # ── JetStream ─────────────────────────────────────────────────────────────────────

    def jetstream(self) -> JetStreamContext:
        """The JetStream context on this connection (local; no round-trip)."""
        return self.client.jetstream()

    async def ensure_stream(
        self,
        name: str,
        subjects: list[str],
        *,
        max_age_s: float,
        max_bytes: int,
    ) -> None:
        """Provision the JetStream stream *name* over *subjects*. Idempotent.

        Called on every boot, so "it already exists" is the *normal* path, not an error:
        ``add_stream`` succeeds unchanged when a stream with an identical config is already
        there, and only fails when one exists with a *different* config — which is then an
        upgrade, so we update it in place rather than refusing to start.

        The retention shape is fixed, not configurable, and each part of it is load-bearing:

        * ``limits`` retention — a message lives out ``max_age_s`` regardless of who has
          acked it. The alternatives delete on ack (``workqueue``/``interest``), which sounds
          tidier and is a trap: with ``interest``, a message published while no durable
          consumer exists is dropped *on arrival*, so the exact window this stream is meant
          to cover (the consumer is down) is the one it would not cover.
        * ``discard: old`` — at the limit, the oldest message is evicted rather than the new
          publish refused. Publishers here do not read acks (see the module docstring), so a
          refusal would be silent at the source; dropping the oldest at least fails in the
          direction of the freshest truth.
        * ``file`` storage — the point is surviving a restart, and memory storage does not
          survive the *server's*.
        * ``no_ack`` — **required, not chosen.** Tenant-scoped subjects put the tenant in the
          leading token, so any stream over them starts with ``*``, and NATS treats a leading
          ``*`` as overlapping its own reserved ``$JS.>`` namespace. It refuses such a stream
          outright unless ``no_ack`` is set (``err_code 10052``: *subjects that overlap with
          jetstream api require no-ack to be true*). What ``no_ack`` disables is the
          **publisher's** ack — which this bus never asked for, since :meth:`publish` is
          plain core NATS on every path. Consumer acks are a property of the consumer, not
          the stream, and are unaffected. The real consequence is a hard one worth stating:
          publisher-side acks are *impossible* for a tenant-first subject scheme, so
          "should emitters use ``js.publish``?" is not a trade-off here — it is a subject
          scheme change, which is a contract change (ADR-0103 §2), not a transport one.

        Raises whatever JetStream raised if the stream can neither be created nor updated
        *and* the existing one does not cover *subjects* — that is unrecoverable silence
        (messages land nowhere) and must not be swallowed at boot.
        """
        js = self.jetstream()
        config = StreamConfig(
            name=name,
            subjects=list(subjects),
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.OLD,
            storage=StorageType.FILE,
            max_age=max_age_s,
            max_bytes=max_bytes,
            no_ack=True,
        )
        try:
            await js.add_stream(config)
        except APIError as exc:
            log.info(
                "jetstream stream exists with a different config; updating",
                stream=name,
                error=str(exc),
            )
        else:
            log.info("jetstream stream ensured", stream=name, subjects=subjects)
            return

        try:
            await js.update_stream(config)
        except APIError as exc:
            # Some fields are immutable once a stream exists (storage type, retention
            # policy). An operator's pre-existing stream that still *routes* our subjects
            # is usable — a tuning mismatch is not worth refusing to boot over. One that
            # does not route them is fatal: every event would land nowhere, silently.
            info = await js.stream_info(name)
            existing = set(info.config.subjects or [])
            if not set(subjects) <= existing:
                raise
            log.warning(
                "jetstream stream kept as-is; its config could not be updated",
                stream=name,
                subjects=sorted(existing),
                error=str(exc),
            )
        else:
            log.info("jetstream stream updated", stream=name, subjects=subjects)

    async def pull_subscribe_any_tenant(
        self,
        subject: str,
        *,
        durable: str,
        stream: str,
        ack_wait_s: float,
    ) -> JetStreamContext.PullSubscription:
        """A durable JetStream pull subscription over ``*.<subject>`` — **every tenant**.

        The JetStream counterpart of :meth:`subscribe_any_tenant`, and core-only for the same
        reason (ADR-0066): the single-token wildcard sits where
        :func:`~epicurus_core.tenancy.scope_subject` puts the tenant, and reaching across
        tenants is the core's job alone.

        *durable* is a server-side cursor that outlives this process — that is the whole
        point. A restarted consumer binds the existing durable and resumes exactly where its
        last ack left off; a brand-new one starts at the head of the stream and replays
        everything still in it (harmless, because the consumer's own store dedups).

        Acks are **explicit** and redelivery is **unlimited** (``max_deliver = -1``). Both are
        deliberate: an unacked message must come back, and a delivery cap would turn a long
        database outage into permanent, silent data loss — the exact failure this whole
        transport exists to remove. A message that can never be *stored* must therefore be
        terminated by the consumer (``msg.term()``), not left to retry forever.
        """
        scoped = f"*.{subject}"
        return await self.jetstream().pull_subscribe(
            scoped,
            durable=durable,
            stream=stream,
            config=ConsumerConfig(
                durable_name=durable,
                filter_subject=scoped,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=ack_wait_s,
                max_deliver=-1,
            ),
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
