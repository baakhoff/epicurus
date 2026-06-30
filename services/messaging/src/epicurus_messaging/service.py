"""The messaging module: a provider-pluggable chat bridge (ADR-0058 / ADR-0062).

It carries the two ends of the inbox contract for external channels (Telegram, Discord, …):
it publishes :data:`~epicurus_core.MESSAGING_INBOUND` when a bridge receives a message, and
consumes :data:`~epicurus_core.MESSAGING_OUTBOUND` to deliver the agent's reply. The core
owns the turn in between; this module never calls an LLM (constraint #8).

It exposes **no agent tools** — it is a transport, driven by NATS, not a capability the agent
invokes. :func:`build_bridges` assembles the :class:`~epicurus_messaging.manager.BridgeManager`
that runs every bridge at once: the always-on in-process loopback echo plus each real bridge
(dormant until the operator connects it). Each bridge declares the OpenBao secrets it needs via
the provider seam, which flow into the manifest ``secrets[]``.
"""

from __future__ import annotations

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    EpicurusModule,
    SecretStore,
    UiSection,
)
from epicurus_messaging.discord_provider import DiscordProvider
from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.manager import BridgeManager
from epicurus_messaging.providers import BridgeProvider
from epicurus_messaging.settings import MessagingSettings
from epicurus_messaging.telegram_provider import TelegramProvider

MODULE_NAME = "messaging"


def build_bridges(settings: MessagingSettings, secrets: SecretStore) -> BridgeManager:
    """Assemble every bridge the module runs (ADR-0062, provider seam ADR-0016).

    The in-process **loopback** echo is always present (no token, so a fresh install has a
    working path and smoke can prove inbound → turn → outbound). Each **real** bridge is
    constructed too but stays dormant until its per-tenant token is stored in OpenBao — the
    operator connects it from the web surface (#369), which triggers a reload so it connects
    without a module restart. Adding a bridge (Telegram #365, …) is one more line here.
    """
    tenant = settings.default_tenant_id
    loopback = LoopbackProvider()
    providers: list[BridgeProvider] = [
        loopback,
        DiscordProvider(secrets=secrets, tenant=tenant),
        TelegramProvider(
            secrets,
            tenant=tenant,
            api_base=settings.telegram_api_base,
            poll_timeout=settings.telegram_poll_timeout,
        ),
    ]
    return BridgeManager(providers, loopback=loopback)


def build_module(manager: BridgeManager) -> EpicurusModule:
    """Build the messaging module — declares its events and the bridges' secrets, no tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.2.0",
        description=(
            "Chat bridges: connect external messaging channels (Telegram, Discord, …) to the "
            "assistant — inbound messages drive an agent turn and replies route back out."
        ),
        # The union of per-tenant bot tokens the real bridges need (OpenBao); loopback needs none.
        secrets=manager.secret_names(),
        ui=UiSection(
            icon="message",
            summary=(
                "Bridges external chat channels to the assistant so you can talk to it from "
                "messaging apps. Connect a bridge (Discord, …) by storing its bot token; the "
                "in-process loopback bridge is always available for development."
            ),
            status_url="/status",
        ),
    )
    module.emits(
        MESSAGING_INBOUND, "A normalized message received from an external channel (→ the core)."
    )
    module.consumes(
        MESSAGING_OUTBOUND, "An agent reply (from the core) to deliver via the matching bridge."
    )
    return module
