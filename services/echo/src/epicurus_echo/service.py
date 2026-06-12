"""The echo module: an ``echo`` MCP tool plus a NATS request/reply responder.

Together these exercise both halves of the module↔core contract — the agent-facing
MCP tool surface and the NATS event path — which is what makes echo the contract
proof and the reference a new module is modeled on.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, Event, EventBus, UiAction, UiSection

ECHO_SUBJECT = "echo.request"


def build_module() -> EpicurusModule:
    """Build the echo module and register its tool and declared events."""
    module = EpicurusModule(
        "echo",
        version="0.1.0",
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
    )

    @module.tool()
    def echo(message: str) -> str:
        """Return the given message unchanged."""
        return message

    module.consumes(ECHO_SUBJECT, "request/reply: echoes the payload back")
    return module


async def echo_responder(event: Event) -> bytes:
    """Reply handler for `echo.request`: echo the request's raw payload back."""
    return event.data


async def serve_responder(bus: EventBus, tenant_id: str) -> None:
    """Register the echo request/reply responder on the tenant-scoped subject."""
    await bus.reply(ECHO_SUBJECT, echo_responder, tenant_id=tenant_id)
