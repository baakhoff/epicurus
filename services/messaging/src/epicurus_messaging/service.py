"""The messaging module: a provider-pluggable chat bridge (ADR-0058).

It carries the two ends of the inbox contract for external channels (Telegram, Discord, …):
it publishes :data:`~epicurus_core.MESSAGING_INBOUND` when a bridge receives a message, and
consumes :data:`~epicurus_core.MESSAGING_OUTBOUND` to deliver the agent's reply. The core
owns the turn in between; this module never calls an LLM (constraint #8).

It exposes **no agent tools** — it is a transport, driven by NATS, not a capability the agent
invokes. The active bridge is chosen by :func:`build_provider` (default: the in-process
loopback echo); each bridge declares the OpenBao secrets it needs via the provider seam, which
flow into the manifest ``secrets[]``.
"""

from __future__ import annotations

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    EpicurusModule,
    SecretStore,
    UiSection,
)
from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.providers import BridgeProvider
from epicurus_messaging.settings import MessagingSettings

MODULE_NAME = "messaging"


def build_provider(settings: MessagingSettings, secrets: SecretStore) -> BridgeProvider:
    """Select the active bridge provider from settings (ADR-0016 provider seam).

    ``secrets`` is threaded in for the bridges that need a per-tenant token (Telegram #365,
    Discord #366, …); the built-in loopback bridge needs none. An unknown name fails loudly
    rather than silently running with no bridge.
    """
    name = settings.messaging_provider.strip().lower()
    if name in ("", "loopback"):
        return LoopbackProvider()
    raise ValueError(
        f"unknown messaging provider {name!r}; only 'loopback' ships in the foundation "
        "(Telegram/Discord/Slack/WhatsApp bridges fan out after #364)"
    )


def build_module(provider: BridgeProvider) -> EpicurusModule:
    """Build the messaging module — declares its events and the active bridge, no tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
        description=(
            "Chat bridges: connect external messaging channels (Telegram, Discord, …) to the "
            "assistant — inbound messages drive an agent turn and replies route back out."
        ),
        # Per-tenant bot tokens the active bridge needs (OpenBao); empty for loopback.
        secrets=provider.secret_names(),
        ui=UiSection(
            icon="message",
            summary=(
                "Bridges external chat channels to the assistant so you can talk to it from "
                f"messaging apps. Active bridge: {provider.provider_name()}."
            ),
            status_url="/status",
        ),
    )
    module.emits(
        MESSAGING_INBOUND, "A normalized message received from an external channel (→ the core)."
    )
    module.consumes(
        MESSAGING_OUTBOUND, "An agent reply (from the core) to deliver via the active bridge."
    )
    return module
