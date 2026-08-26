"""The module event spine — one standardized envelope for world-change announcements.

A module *emits* when something changed in the world it owns: mail arrived, a calendar
event moved, a note was saved. It says only that the change happened and where to look —
it never says what should be done about it. Deciding that is the automations engine's
job, and keeping the two apart is what lets a module emit before any consumer exists
(with nothing subscribed, emitting is free) and lets a consumer be written once against
every module instead of once per module.

## The envelope

``tenant_id · module · type · occurred_at · dedup_key · entity_ref? · payload ·
schema_version``. Two of those carry more weight than they look:

* **``dedup_key``** is the emitter's own idempotency key for the *change*, not for the
  message — a poll loop that re-sees the same mail must reuse the same key so the intake
  stores one row. Scope it to the module (the log dedups on tenant+module+key), and make
  it deterministic: a provider id (``"gmail:18f2c1"``) beats a uuid, which defeats the
  whole mechanism by being different every time.
* **``payload``** is **pointers and minimal metadata, never content**. An id, a subject
  line, a count — enough for a filter to match and a feed row to read. Never a mail body,
  never document text, never a credential. A consumer that needs the real thing fetches
  it through the owning module's tools under its own authorization, which is what keeps
  the log from becoming a second, unguarded copy of every module's data.

That discipline is **enforced here, not just documented** (:data:`MAX_PAYLOAD_BYTES` and
a credential-shaped-key rejection) — the same posture ``UiAction`` takes with its
danger/confirm rule. A rule a module author can accidentally ignore is a rule the log
will eventually violate.

One consequence to know before you hit it: the credential screen
(:mod:`epicurus_core.redaction`) matches key *names* by blunt substring, so a payload key
containing ``key``, ``token``, ``auth``, or ``secret`` is refused **even when it holds
nothing sensitive** — ``idempotency_key`` and ``sort_key`` are rejected exactly like
``api_key``. That is the intended trade (a false positive costs a rename; a false negative
leaks a credential into a browser tab), and the rejection is loud so you find out at your
call site. Name the field for what it points at — ``message_id``, not ``message_key`` — and
never repeat an envelope field in the payload: ``dedup_key`` is already carried above.

## Subjects

An event's ``type`` *is* its subject suffix: type ``mail.received`` publishes to the base
subject ``events.mail.received``, which :class:`~epicurus_core.events.EventBus` scopes to
``<tenant>.events.mail.received`` at publish time
(:func:`~epicurus_core.tenancy.scope_subject`). The ``events.`` prefix is what makes the
spine subscribable as a whole: the core's intake takes ``events.>`` and gets every module
event, while the bus's existing non-envelope traffic (``echo.request``, ``llm.usage``,
``notes.saved``, ``messaging.inbound``) keeps its own top-level names and stays out of the
log. Aligning with those live conventions rather than inventing a scheme is deliberate —
the prefix is the *only* thing added to them.

``type`` must start with ``<module>.``, so a subject is self-describing and a
module/type typo fails at emit instead of silently mis-attributing an event in the
catalog. Relaxing that later is backward-compatible; tightening it later would not be.

## Delivery

**At-least-once from the NATS server to the core's durable log.** The spine's subjects are
covered by a JetStream stream (:data:`EVENTS_STREAM`), and the core consumes it through a
durable pull consumer (:data:`EVENTS_DURABLE`) that acks a message only once the row exists
in Postgres. A core that is down — restarting, deploying, crashed — misses nothing: the
stream holds the events, and the durable cursor resumes where the last ack left off.
Redelivery is safe by construction, because ``dedup_key`` is unique in the log.

**Best-effort from the emitter to the NATS server.** Emitting is still a plain core-NATS
publish: :func:`emit_event` returns once the local NATS client has accepted the message for
transmission, *not* once the server has stored it. Nothing about the emitter changed, which
is the point — persistence here is a property of the subject, not of the publisher's API.
What that leaves open, stated plainly:

* **NATS unreachable at publish.** nats-py buffers into its pending queue and flushes on
  reconnect; an emitter that dies before the flush loses those events. A client that is
  closed — never connected, or already shut down — raises instead, so *that* one the caller
  does see.
* **Stream at its limit.** The stream discards *old* messages to accept new ones, so a
  runaway emitter costs the oldest unconsumed events, silently, rather than the newest.
* **Unclean server crash.** A message the server accepted but had not yet written to the
  stream file can be lost.

All three need NATS itself to be down or dying, which takes every other module↔core path
with it; the failure this design exists to remove — *the core restarted and the world's
changes vanished* — is closed. Publisher-side acks (``js.publish``) would close the rest, at
the cost of a round-trip on every module hot path and an emit that fails outright until the
core has provisioned the stream. See the ADR for why that trade was not taken at 1.0.

The durable log stays the copy of record regardless: "what happened" is a question you ask
Postgres, not the bus. The stream is a delivery buffer in front of it, not a second archive.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator, model_validator

from epicurus_core.contracts import EntityRef
from epicurus_core.redaction import secret_keys_in
from epicurus_core.tenancy import TenantError, validate_tenant_id

if TYPE_CHECKING:  # pragma: no cover - import cycle guard; EventBus imports nothing here
    from epicurus_core.events import EventBus

__all__ = [
    "EVENTS_ACK_WAIT_S",
    "EVENTS_DURABLE",
    "EVENTS_PREFIX",
    "EVENTS_STREAM",
    "EVENTS_STREAM_MAX_AGE_S",
    "EVENTS_STREAM_MAX_BYTES",
    "EVENTS_STREAM_SUBJECT",
    "EVENTS_WILDCARD",
    "MAX_PAYLOAD_BYTES",
    "SCHEMA_VERSION",
    "EventEnvelope",
    "emit_event",
    "event_subject",
]

EVENTS_PREFIX = "events"
"""The spine's own subject namespace — keeps envelope traffic separate from the bus's
existing per-module subjects (``echo.request``, ``llm.usage``, …)."""

EVENTS_WILDCARD = f"{EVENTS_PREFIX}.>"
"""Every module event, for the core's intake. Tenant-scoped to ``<tenant>.events.>``."""

EVENTS_STREAM = "EPICURUS_EVENTS"
"""The JetStream stream that persists the spine. Uppercase by NATS convention (stream names
are an operator-facing namespace, not a subject), and prefixed so an operator sharing a
server can tell whose stream it is."""

EVENTS_STREAM_SUBJECT = f"*.{EVENTS_WILDCARD}"
"""What the stream captures: ``*.events.>`` — every tenant's spine and nothing else.

Deliberately the same wildcard the intake subscribes, and for the same reason: the
single-token ``*`` sits exactly where :func:`~epicurus_core.tenancy.scope_subject` puts the
tenant. A stream scoped to one tenant would violate constraint #1 the moment a second tenant
existed, and a broader stream (``>``) would swallow ``llm.usage``, ``echo.request`` and every
other non-envelope subject into a persisted log they were never meant to enter."""

EVENTS_DURABLE = "core-event-intake"
"""The core's durable consumer on :data:`EVENTS_STREAM` — the cursor that makes a restart
survivable. Named for the consumer, not the release, because its identity must not change
across upgrades: a renamed durable is a *new* cursor, which silently replays the whole
stream. Hyphens only; NATS forbids ``.``, ``*`` and ``>`` in a durable name."""

EVENTS_STREAM_MAX_AGE_S = 7 * 24 * 60 * 60.0
"""How long the stream holds an event: 7 days.

The stream is a delivery buffer, not the archive — the archive is the ``module_events``
table, on its own (longer, operator-set) retention. Sized so that a core down for a weekend
still catches up, and short enough that the buffer never becomes a second copy of the log
that nobody prunes."""

EVENTS_STREAM_MAX_BYTES = 512 * 1024 * 1024
"""Hard disk ceiling for the stream: 512 MiB — roughly 100k events at the 4 KiB payload cap.

A bound is not optional: without one, a module stuck in an emit loop fills the NATS volume
and takes down every other subject with it. At the ceiling the stream discards its oldest
messages (see :meth:`~epicurus_core.events.EventBus.ensure_stream`)."""

EVENTS_ACK_WAIT_S = 30.0
"""How long JetStream waits for the core's ack before redelivering. Sized well above a
Postgres insert and well below an operator's patience — long enough that a slow write is not
mistaken for a dead consumer, short enough that a consumer killed mid-message gets its work
back promptly."""

SCHEMA_VERSION = 1
"""Current envelope schema. Bumped only on a *breaking* shape change; additive optional
fields do not bump it, so a consumer pinned to 1 keeps working."""

MAX_PAYLOAD_BYTES = 4096
"""Serialized-payload ceiling. Sized to fit ids, a subject line, and a handful of counts,
and to *not* fit a mail body or a document — the cap is how "pointers, not content"
stops being a request and starts being a contract."""

# One NATS subject token: lowercase alphanumerics, underscores, hyphens. Excludes "."
# (the separator), "*" and ">" (wildcards — a module must not be able to widen its own
# subject), and whitespace. Matches every subject the bus already carries.
_TOKEN = r"[a-z0-9][a-z0-9_-]*"
_MODULE_RE = re.compile(rf"^{_TOKEN}$")
_TYPE_RE = re.compile(rf"^{_TOKEN}(?:\.{_TOKEN})+$")


def event_subject(event_type: str) -> str:
    """The *base* subject an event of *event_type* publishes to: ``events.<type>``.

    Base, not final: :class:`~epicurus_core.events.EventBus` tenant-scopes it at publish
    time, so the wire subject is ``<tenant>.events.<type>``.
    """
    return f"{EVENTS_PREFIX}.{event_type}"


class EventEnvelope(BaseModel):
    """One world-change announcement — the shape every module event takes.

    Construct it directly only to *read* an event off the wire
    (``EventEnvelope.model_validate_json(msg.data)``); to publish one, use
    :func:`emit_event`, which builds and validates this and picks the subject.
    """

    # The length bounds below are not cosmetic: they mirror the core's `module_events`
    # columns exactly (String(64) / String(128) / String(255)). SQLite ignores VARCHAR
    # lengths, so an over-long value passes every unit test and then fails on Postgres —
    # at *intake*, inside a background subscriber, nowhere near the emitter that caused
    # it. Bounding them here rejects it at the module author's call site instead.
    schema_version: int = SCHEMA_VERSION
    tenant_id: str  # ≤63 by the tenant id rules themselves (see _valid_tenant)
    module: str = Field(min_length=1, max_length=64)
    # Dotted and prefixed with the module: "mail.received", "echo.pinged". This is also
    # the subject suffix (see event_subject).
    type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    # The emitter's idempotency key for the underlying change; the durable log dedups on
    # (tenant, module, dedup_key). Deterministic per change — never a fresh uuid.
    dedup_key: str = Field(min_length=1, max_length=255)
    # The entity this event is about (ADR-0019), so a feed row or a notification renders
    # a hover-card chip with no per-module code in the consumer.
    entity_ref: EntityRef | None = None
    # Pointers + minimal metadata only. Capped and credential-screened below.
    payload: dict[str, Any] = Field(default_factory=dict)
    # Set only on an event produced *by* an automation run: the id of the run that caused
    # it (ADR-0105's loop guard). A module emitter never sets this — it announces a change
    # in the world, which has no cause inside the system. The automations matcher refuses
    # to trigger on an event that carries one, which is the depth-1 hard stop that keeps an
    # automation from feeding itself. Additive and optional, so schema_version stays 1.
    causation_id: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def _valid_tenant(cls, value: str) -> str:
        # Constraint #1: an event that cannot name a well-formed tenant cannot be scoped,
        # logged, or metered, so it must never reach the bus.
        try:
            return validate_tenant_id(value)
        except TenantError as exc:
            # validate_tenant_id raises TenantError (a RuntimeError), which pydantic does
            # not wrap — only ValueError/AssertionError become a ValidationError. Left
            # alone, a bad tenant would escape emit_event as a different exception type
            # than every other envelope violation. Translate it so a module author has
            # exactly one thing to catch.
            raise ValueError(str(exc)) from exc

    @field_validator("module")
    @classmethod
    def _valid_module(cls, value: str) -> str:
        if not _MODULE_RE.fullmatch(value):
            raise ValueError(
                f"invalid module {value!r}: one lowercase subject token "
                "(alphanumerics, underscore, hyphen), no dots or wildcards"
            )
        return value

    @field_validator("type")
    @classmethod
    def _valid_type(cls, value: str) -> str:
        if not _TYPE_RE.fullmatch(value):
            raise ValueError(
                f"invalid event type {value!r}: two or more dotted lowercase tokens "
                "(e.g. 'mail.received'), no wildcards"
            )
        return value

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        # A naive timestamp is ambiguous the moment it crosses a process boundary, and
        # this one crosses two (bus, then a tenant-timezone-aware digest window).
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _type_is_module_prefixed(self) -> EventEnvelope:
        if not self.type.startswith(f"{self.module}."):
            raise ValueError(
                f"event type {self.type!r} must start with its module ({self.module!r}), "
                f"e.g. {self.module}.something — the subject is self-describing"
            )
        return self

    @model_validator(mode="after")
    def _payload_is_a_pointer(self) -> EventEnvelope:
        leaked = secret_keys_in(self.payload)
        if leaked:
            raise ValueError(
                f"event {self.type!r} payload carries credential-shaped keys {leaked}: "
                "an event names what changed and where to look, never a secret"
            )
        try:
            size = len(json.dumps(self.payload).encode())
        except TypeError as exc:  # a value pydantic accepted as Any but cannot serialize
            raise ValueError(
                f"event {self.type!r} payload must be JSON-serializable: {exc}"
            ) from exc
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"event {self.type!r} payload is {size} bytes, over the "
                f"{MAX_PAYLOAD_BYTES}-byte cap: emit pointers and metadata, not content — "
                "a consumer fetches the body through your module's tools"
            )
        return self

    def subject(self) -> str:
        """This event's base subject (``events.<type>``); see :func:`event_subject`."""
        return event_subject(self.type)


async def emit_event(
    bus: EventBus,
    *,
    tenant_id: str,
    module: str,
    event_type: str,
    dedup_key: str,
    payload: dict[str, Any] | None = None,
    entity_ref: EntityRef | None = None,
    occurred_at: datetime | None = None,
    causation_id: str | None = None,
) -> EventEnvelope:
    """Announce a world change on the tenant-scoped spine; returns what was published.

    The one way a module emits::

        await emit_event(
            bus,
            tenant_id=tenant,
            module="mail",
            event_type="mail.received",
            dedup_key=f"gmail:{msg.id}",          # deterministic per change
            payload={"message_id": msg.id, "subject": msg.subject, "unread": 1},
            entity_ref=EntityRef(ref_id=msg.id, module="mail", kind="message", title=msg.subject),
        )

    *event_type* maps to the envelope's ``type`` field (the parameter avoids shadowing the
    builtin at every call site). *occurred_at* defaults to now, UTC — pass it explicitly
    when the change happened earlier than the moment you noticed it, since that is the
    timestamp a digest window and the feed order by.

    *causation_id* is **core-only**: a module leaves it unset, because a change in the world
    has no cause inside the system. The automations runner stamps it on events produced by a
    run, and the matcher then refuses to trigger on them — the loop guard (ADR-0105).

    Raises ``ValueError`` (via the envelope's validators) on a malformed type, a
    mismatched module prefix, an oversized payload, or a credential-shaped payload key —
    before anything reaches the bus.

    Publishing itself is unacknowledged: this returns once the local NATS client accepts the
    message, not once the server has stored it and certainly not once anything has consumed
    it. Once the server *does* have it, delivery to the core's durable log is at-least-once
    and survives a core restart — see **Delivery** in this module's docstring for the three
    windows that stay open on the publish side.
    """
    envelope = EventEnvelope(
        tenant_id=tenant_id,
        module=module,
        type=event_type,
        occurred_at=occurred_at or datetime.now(UTC),
        dedup_key=dedup_key,
        entity_ref=entity_ref,
        payload=payload or {},
        causation_id=causation_id,
    )
    await bus.publish(envelope.subject(), envelope.model_dump(mode="json"), tenant_id=tenant_id)
    return envelope
