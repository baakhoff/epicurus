"""Tests for the module event spine — the envelope's contract and the emit helper.

The envelope's validators *are* the spine's contract: everything downstream (the durable
log, the feed, the automations matcher) trusts that a parsed envelope is well-formed,
tenant-scoped, and free of content and credentials. So each rule gets a test that proves
it rejects, not merely that the happy path passes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from epicurus_core import EntityRef
from epicurus_core.module_events import (
    EVENTS_WILDCARD,
    MAX_PAYLOAD_BYTES,
    SCHEMA_VERSION,
    EventEnvelope,
    emit_event,
    event_subject,
)

TENANT = "local"


def _envelope(**overrides: object) -> EventEnvelope:
    """A valid envelope, with *overrides* applied — so a test names only what it varies."""
    fields: dict[str, object] = {
        "tenant_id": TENANT,
        "module": "echo",
        "type": "echo.pinged",
        "occurred_at": datetime.now(UTC),
        "dedup_key": "abc123",
    }
    fields.update(overrides)
    return EventEnvelope(**fields)  # type: ignore[arg-type]


class _RecordingBus:
    """Captures what would have gone on the wire, so emit_event is testable without NATS."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object, str | None]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        self.published.append((subject, data, tenant_id))


# ── subjects ─────────────────────────────────────────────────────────────────


def test_event_subject_prefixes_the_spine_namespace() -> None:
    assert event_subject("mail.received") == "events.mail.received"


def test_wildcard_matches_the_prefix() -> None:
    # The core's intake takes this; if the prefix and the wildcard ever disagree, intake
    # silently hears nothing — so pin them to each other.
    assert EVENTS_WILDCARD == "events.>"
    assert event_subject("echo.pinged").startswith(EVENTS_WILDCARD[:-1])


def test_envelope_subject_matches_the_helper() -> None:
    assert _envelope().subject() == event_subject("echo.pinged") == "events.echo.pinged"


# ── the envelope's contract ──────────────────────────────────────────────────


def test_valid_envelope_round_trips_through_json() -> None:
    ref = EntityRef(ref_id="e1", module="echo", kind="ping", title="hi")
    original = _envelope(entity_ref=ref, payload={"n": 1})
    parsed = EventEnvelope.model_validate_json(original.model_dump_json())
    assert parsed == original
    assert parsed.schema_version == SCHEMA_VERSION


def test_defaults_are_current_schema_and_empty_payload() -> None:
    envelope = _envelope()
    assert envelope.schema_version == SCHEMA_VERSION
    assert envelope.payload == {}
    assert envelope.entity_ref is None


@pytest.mark.parametrize("tenant", ["", "Bad-Upper", "-lead", "trail-", "has space", "a" * 64])
def test_rejects_malformed_tenant(tenant: str) -> None:
    # Constraint #1: an event that cannot name a well-formed tenant cannot be scoped,
    # logged, or metered — so it must never be constructible.
    with pytest.raises(ValidationError):
        _envelope(tenant_id=tenant)


@pytest.mark.parametrize("module", ["", "has.dot", "UPPER", "has space", "*", ">"])
def test_rejects_malformed_module(module: str) -> None:
    with pytest.raises(ValidationError):
        _envelope(module=module, type=f"{module}.thing")


@pytest.mark.parametrize(
    "event_type",
    [
        "nodots",  # a single token is not a dotted type
        "echo.",  # trailing separator
        ".pinged",  # leading separator
        "echo..pinged",  # empty token
        "echo.PINGED",  # uppercase
        "echo.has space",
    ],
)
def test_rejects_malformed_type(event_type: str) -> None:
    with pytest.raises(ValidationError):
        _envelope(type=event_type)


@pytest.mark.parametrize("event_type", ["echo.*", "echo.>", "echo.a.>"])
def test_rejects_wildcard_in_type(event_type: str) -> None:
    # The type becomes the subject suffix. A module that could smuggle a wildcard into it
    # could publish onto — or subscribe across — subjects that are not its own.
    with pytest.raises(ValidationError):
        _envelope(type=event_type)


def test_rejects_type_not_prefixed_by_its_module() -> None:
    with pytest.raises(ValidationError, match="must start with its module"):
        _envelope(module="echo", type="mail.received")


def test_accepts_a_deeper_dotted_type() -> None:
    # "knowledge.index.completed" is a real shape on this bus; the rule is a module
    # prefix, not exactly two tokens.
    envelope = _envelope(module="knowledge", type="knowledge.index.completed")
    assert envelope.subject() == "events.knowledge.index.completed"


def test_rejects_naive_occurred_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _envelope(occurred_at=datetime(2026, 7, 17, 12, 0, 0))


def test_rejects_blank_dedup_key() -> None:
    with pytest.raises(ValidationError):
        _envelope(dedup_key="")


def test_rejects_an_over_long_dedup_key() -> None:
    with pytest.raises(ValidationError):
        _envelope(dedup_key="k" * 256)


def test_rejects_an_over_long_module_and_type() -> None:
    # These bounds mirror the core's module_events columns. SQLite ignores VARCHAR
    # lengths, so without them an over-long value passes every unit test and then fails on
    # Postgres at intake — in a background subscriber, far from the emitter at fault.
    long_module = "m" * 65
    with pytest.raises(ValidationError):
        _envelope(module=long_module, type=f"{long_module}.thing")
    with pytest.raises(ValidationError):
        _envelope(module="echo", type="echo." + "x" * 130)


def test_accepts_a_past_occurred_at() -> None:
    # A module may report a change it noticed late; the envelope must not insist the
    # timestamp is "now" — the digest window depends on the real one.
    earlier = datetime.now(UTC) - timedelta(hours=3)
    assert _envelope(occurred_at=earlier).occurred_at == earlier


# ── payload discipline: enforced, not requested ──────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "api_key",
        "access_token",
        "Authorization",
        "PASSWORD",
        "client_secret",
        "credential",
    ],
)
def test_rejects_credential_shaped_payload_key(key: str) -> None:
    with pytest.raises(ValidationError, match="credential-shaped"):
        _envelope(payload={key: "hunter2"})


def test_rejects_oversized_payload() -> None:
    # The cap is what turns "pointers, not content" from a request into a contract: a mail
    # body cannot fit through it.
    with pytest.raises(ValidationError, match="over the"):
        _envelope(payload={"body": "x" * (MAX_PAYLOAD_BYTES + 1)})


def test_accepts_a_payload_at_the_cap() -> None:
    # Boundary: the limit rejects what is *over* it, not what reaches it.
    filler = "x" * (MAX_PAYLOAD_BYTES - len('{"body": ""}'))
    envelope = _envelope(payload={"body": filler})
    assert len(envelope.model_dump_json()) > 0


def test_rejects_unserializable_payload() -> None:
    # pydantic accepts anything for dict[str, Any]; this would explode later at publish,
    # in a module's log, far from the line that caused it. Fail at construction instead.
    with pytest.raises(ValidationError, match="JSON-serializable"):
        _envelope(payload={"when": object()})


def test_a_pointer_payload_is_fine() -> None:
    envelope = _envelope(payload={"message_id": "18f2c1", "subject": "Re: lunch", "unread": 3})
    assert envelope.payload["message_id"] == "18f2c1"


# ── emit_event ───────────────────────────────────────────────────────────────


async def test_emit_publishes_the_scoped_subject_and_returns_the_envelope() -> None:
    bus = _RecordingBus()
    envelope = await emit_event(
        bus,  # type: ignore[arg-type]  # structural: only publish() is used
        tenant_id=TENANT,
        module="echo",
        event_type="echo.pinged",
        dedup_key="k1",
        payload={"n": 1},
    )
    assert len(bus.published) == 1
    subject, data, tenant = bus.published[0]
    # The base subject + the tenant go to the bus separately; EventBus scopes them.
    assert subject == "events.echo.pinged"
    assert tenant == TENANT
    assert isinstance(data, dict)
    assert data["type"] == "echo.pinged"
    assert data["tenant_id"] == TENANT
    assert envelope.dedup_key == "k1"


async def test_emit_serializes_json_safely() -> None:
    # The bus JSON-encodes whatever dict it is handed, so datetimes must already be
    # strings by then — mode="json", not a raw model_dump.
    bus = _RecordingBus()
    ref = EntityRef(ref_id="e1", module="echo", kind="ping", title="hi")
    await emit_event(
        bus,  # type: ignore[arg-type]
        tenant_id=TENANT,
        module="echo",
        event_type="echo.pinged",
        dedup_key="k1",
        entity_ref=ref,
    )
    _, data, _ = bus.published[0]
    assert isinstance(data, dict)
    assert isinstance(data["occurred_at"], str)
    assert data["entity_ref"]["ref_id"] == "e1"
    # And what lands on the wire must parse back into an envelope — the intake's job.
    assert EventEnvelope.model_validate(data).entity_ref == ref


async def test_emit_defaults_occurred_at_to_now() -> None:
    bus = _RecordingBus()
    before = datetime.now(UTC)
    envelope = await emit_event(
        bus,  # type: ignore[arg-type]
        tenant_id=TENANT,
        module="echo",
        event_type="echo.pinged",
        dedup_key="k1",
    )
    assert before <= envelope.occurred_at <= datetime.now(UTC)


async def test_emit_rejects_before_publishing() -> None:
    # The validators must fire *before* the bus is touched: a bad event should never be
    # half-emitted, and the module author should see the error at their call site.
    bus = _RecordingBus()
    with pytest.raises(ValidationError):
        await emit_event(
            bus,  # type: ignore[arg-type]
            tenant_id=TENANT,
            module="echo",
            event_type="mail.received",  # not echo's to emit
            dedup_key="k1",
        )
    assert bus.published == []
