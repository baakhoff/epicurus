"""The echo module: an ``echo`` MCP tool, a NATS request/reply responder, and a spine emitter.

Together these exercise every half of the module↔core contract — the agent-facing MCP tool
surface, the NATS request/reply path, and the event spine — which is what makes echo the
contract proof and the reference a new module is modeled on.

``echo.pinged`` is the spine's reference emitter: the smallest real event there is, so
emit → intake → durable log → feed has something to prove itself against on a fresh stack
(the smoke gate asserts exactly that chain). A real module emits when the world changed;
echo has no world, so it emits when someone pings it.
"""

from __future__ import annotations

import uuid
from typing import Any

from epicurus_core import (
    EntityRef,
    EpicurusModule,
    Event,
    EventBus,
    HoverCard,
    HoverCardDetail,
    PageSpec,
    UiAction,
    UiSection,
    emit_event,
    event_subject,
)

ECHO_SUBJECT = "echo.request"
ECHO_PAGE_ID = "echoes"
ECHO_PINGED = "echo.pinged"
"""The event type echo emits from its demo surface (base subject ``events.echo.pinged``)."""


async def emit_ping(bus: EventBus, *, tenant: str, note: str = "", dedup_key: str = "") -> str:
    """Emit one ``echo.pinged`` onto the spine; returns the dedup key it was filed under.

    A fresh uuid is the *right* dedup key here, and echo is the one place that is true:
    every other emitter reports a change it may re-see (the same mail, polled twice), so
    its key must be derived from the change. A ping has no existence apart from the act of
    pinging — two pings are two changes — so its identity is fresh by definition.

    Passing *dedup_key* overrides that, which is how the demo surface (and the smoke gate)
    can demonstrate the log's idempotency: ping twice with one key, get one event.
    """
    key = dedup_key or uuid.uuid4().hex
    # The payload does not repeat the key: it is already a first-class envelope field, and
    # duplicating it would both be redundant and trip the payload's credential screen —
    # "dedup_key" contains "key". Payload fields carry what the envelope does not.
    payload: dict[str, Any] = {}
    if note:
        # A pointer-sized crumb, not content — the envelope caps the payload and echo
        # models the discipline it is meant to demonstrate.
        payload["note"] = note[:200]
    await emit_event(
        bus,
        tenant_id=tenant,
        module="echo",
        event_type=ECHO_PINGED,
        dedup_key=key,
        payload=payload,
        entity_ref=EntityRef(
            ref_id=key,
            module="echo",
            kind="ping",
            title=note or "ping",
            summary="An echo ping on the module event spine.",
        ),
    )
    return key


def build_module(bus: EventBus | None = None, *, tenant: str = "local") -> EpicurusModule:
    """Build the echo module and register its tools and declared events.

    *bus* wires the spine emitter; the app passes its connected bus. It is optional so a
    caller that only wants the manifest (tests, the installer reading a module's
    descriptor) can build one without standing up NATS — the ping tool then reports the
    spine as unavailable rather than the build failing.
    """
    module = EpicurusModule(
        "echo",
        version="0.4.0",
        description="Echoes messages — proves the MCP tool + NATS event contract.",
        config=["greeting"],
        ui=UiSection(
            summary="The contract-proof module: echoes whatever you send it.",
            config_schema={
                "type": "object",
                "properties": {
                    "greeting": {
                        "type": "string",
                        "title": "Greeting",
                        "description": "Prefix shown by the echo demo.",
                    }
                },
            },
            actions=[
                UiAction(
                    tool="echo",
                    label="Send an echo",
                    description="Round-trip a message through the module.",
                ),
                UiAction(
                    tool="echo_ping",
                    label="Ping the spine",
                    description="Emit an echo.pinged event onto the module event spine.",
                ),
            ],
        ),
        # A left-nav page proving the ADR-0018 bounded-vocabulary contract: echo
        # supplies data only, the shell renders it in the core `browser` archetype.
        pages=[
            PageSpec(
                id=ECHO_PAGE_ID,
                title="Echoes",
                archetype="browser",
                icon="message",
                nav_order=50,
            )
        ],
        # Serves GET /resolve/{kind}/{ref_id} so a referenced echo entity gets a
        # hover-card (ADR-0019) — the reference for the resolver contract.
        resolver=True,
        # Contribute module docs for auto-indexing by the knowledge module (#215).
        docs_url="/module-docs",
    )

    @module.tool()
    def echo(message: str) -> str:
        """Return the given message unchanged."""
        return message

    @module.tool()
    async def echo_ping(note: str = "", dedup_key: str = "") -> str:
        """Announce an ``echo.pinged`` event on the module event spine.

        Args:
            note: an optional short crumb carried in the event payload.
            dedup_key: the event's idempotency key. Two pings sharing one key are a
                single event in the core's log; omit it and every ping is its own.
        """
        if bus is None:
            return "error: the event spine is not wired in this process"
        key = await emit_ping(bus, tenant=tenant, note=note, dedup_key=dedup_key)
        return f"pinged the spine: echo.pinged filed under dedup_key {key}"

    module.consumes(ECHO_SUBJECT, "request/reply: echoes the payload back")
    module.emits(
        event_subject(ECHO_PINGED),
        "someone pinged echo's demo surface — the event spine's reference emitter",
    )
    return module


def echo_page() -> dict[str, Any]:
    """The ``browser`` archetype data for echo's left-nav page (ADR-0018).

    A module supplies data only; the core shell renders it. This is the browser
    archetype's data shape — a list of items, each with a title, an optional
    subtitle, and a detail ``body`` shown when the item is selected.
    """
    return {
        "title": "Echoes",
        "items": [
            {
                "id": "hello",
                "title": "hello",
                "subtitle": "a friendly echo",
                "body": "hello — echoed back to you, unchanged.",
            },
            {
                "id": "abundance",
                "title": "abundance",
                "subtitle": "the namesake quote",
                "body": (
                    "It is not what we have but what we enjoy that constitutes our abundance."
                ),
            },
            {
                "id": "contract",
                "title": "contract proof",
                "subtitle": "why echo exists",
                "body": (
                    "echo exercises both halves of the module↔core contract: the "
                    "agent-facing MCP tool surface and the NATS event path. This "
                    "page adds the third — a core-rendered module page."
                ),
            },
        ],
    }


def echo_hover_card(kind: str, ref_id: str) -> dict[str, Any]:
    """Resolve an echo entity reference to a hover-card envelope (ADR-0019).

    The reference implementation of the resolver contract: a module returns the
    uniform :class:`HoverCard` shape and the core proxies it to the shell.
    """
    return HoverCard(
        title=ref_id,
        description=f"An echoed {kind}.",
        details=[
            HoverCardDetail(label="kind", value=kind),
            HoverCardDetail(label="id", value=ref_id),
        ],
    ).model_dump()


async def echo_responder(event: Event) -> bytes:
    """Reply handler for `echo.request`: echo the request's raw payload back."""
    return event.data


async def serve_responder(bus: EventBus, tenant_id: str) -> None:
    """Register the echo request/reply responder on the tenant-scoped subject."""
    await bus.reply(ECHO_SUBJECT, echo_responder, tenant_id=tenant_id)


def echo_docs() -> dict[str, Any]:
    """The echo module's documentation pages for auto-indexing (#215)."""
    return {
        "documents": [
            {
                "path": "overview.md",
                "content": """\
# Echo module

The echo module is the epicurus contract-proof module: it exercises both halves
of the module↔core contract — the MCP tool surface and the NATS event path.

## Tools

### echo

Returns a message unchanged.

**Parameters**
- ``message`` (string) — the text to echo back.

**Returns** the same string.

### echo_ping

Emits an ``echo.pinged`` event onto the module event spine — the reference proof that
emit → intake → durable log → feed works on a fresh stack.

**Parameters**
- ``note`` (string, optional) — a short crumb carried in the event payload.
- ``dedup_key`` (string, optional) — the event's idempotency key. Two pings sharing a
  key are one event in the core's log; omit it and every ping is its own event.

**Returns** a confirmation naming the dedup key the event was filed under.

## Pages

The **Echoes** page (left nav) is rendered by the core using the ``browser``
archetype: the module supplies a list of items; the shell renders them.

## Events

The echo module listens on the ``echo.request`` NATS subject and echoes the
request payload back — a round-trip proof of the NATS request/reply contract.

It also **emits** ``echo.pinged`` (subject ``events.echo.pinged``) on the module
event spine whenever ``echo_ping`` runs — the reference emitter every other module's
events are modeled on. The payload is pointers only (a dedup key and an optional
short note), never content, and the event carries an ``EntityRef`` so the raw events
feed renders it as a hover-card chip with no echo-specific code in the shell.

## Purpose

echo exists as the reference a new module is modeled on: it demonstrates
the tool, event, spine-emit, page, resolver, and docs contracts in one small service.
""",
            }
        ]
    }
