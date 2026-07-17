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

Core NATS pub/sub — at-most-once, fire-and-forget. An event emitted while the core is
down is *gone*, not queued: the bus's JetStream is enabled but this spine does not use it
yet. That is a deliberate v1 posture (see the ADR) and the reason the durable log is the
core's copy of record rather than the bus itself.
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
    "EVENTS_PREFIX",
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

    Raises ``ValueError`` (via the envelope's validators) on a malformed type, a
    mismatched module prefix, an oversized payload, or a credential-shaped payload key —
    before anything reaches the bus. Publishing itself is fire-and-forget: this returns
    once the client accepts the message, not once anything consumes it.
    """
    envelope = EventEnvelope(
        tenant_id=tenant_id,
        module=module,
        type=event_type,
        occurred_at=occurred_at or datetime.now(UTC),
        dedup_key=dedup_key,
        entity_ref=entity_ref,
        payload=payload or {},
    )
    await bus.publish(envelope.subject(), envelope.model_dump(mode="json"), tenant_id=tenant_id)
    return envelope
