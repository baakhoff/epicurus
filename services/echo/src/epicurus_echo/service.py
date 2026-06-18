"""The echo module: an ``echo`` MCP tool plus a NATS request/reply responder.

Together these exercise both halves of the module↔core contract — the agent-facing
MCP tool surface and the NATS event path — which is what makes echo the contract
proof and the reference a new module is modeled on.
"""

from __future__ import annotations

from typing import Any

from epicurus_core import (
    EpicurusModule,
    Event,
    EventBus,
    HoverCard,
    HoverCardDetail,
    PageSpec,
    UiAction,
    UiSection,
)

ECHO_SUBJECT = "echo.request"
ECHO_PAGE_ID = "echoes"


def build_module() -> EpicurusModule:
    """Build the echo module and register its tool and declared events."""
    module = EpicurusModule(
        "echo",
        version="0.2.0",
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
                )
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
        docs_url="/docs",
    )

    @module.tool()
    def echo(message: str) -> str:
        """Return the given message unchanged."""
        return message

    module.consumes(ECHO_SUBJECT, "request/reply: echoes the payload back")
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

## Pages

The **Echoes** page (left nav) is rendered by the core using the ``browser``
archetype: the module supplies a list of items; the shell renders them.

## Events

The echo module listens on the ``echo.request`` NATS subject and echoes the
request payload back — a round-trip proof of the NATS request/reply contract.

## Purpose

echo exists as the reference a new module is modeled on: it demonstrates
the tool, event, page, resolver, and docs contracts in one small service.
""",
            }
        ]
    }
