"""The normalized messaging inbox contract — shared shapes + NATS subjects for the
chat-bridge path (Phase 4, ADR-0058).

An external channel (Telegram, Discord, …) reaches the agent over two tenant-scoped NATS
subjects carried between the ``messaging`` module and the core on the internal network only
(constraint #7):

* a bridge receives a message → the ``messaging`` module publishes an :class:`InboundMessage`
  on :data:`MESSAGING_INBOUND`;
* the core consumes it, runs a **headless** agent turn, and publishes an
  :class:`OutboundMessage` on :data:`MESSAGING_OUTBOUND`;
* the ``messaging`` module consumes that and delivers the reply through the active bridge.

``tenant`` is first-class on both shapes (constraint #1) and the subjects are tenant-scoped
via :func:`~epicurus_core.tenancy.scope_subject`. Conversation identity is derived from the
channel with :func:`session_id_for`, so a bridge thread maps to one persisted session and
memory/facts stay tenant-scoped — one brain across the web UI and every bridge.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "MESSAGING_INBOUND",
    "MESSAGING_OUTBOUND",
    "InboundMessage",
    "MessageAttachment",
    "OutboundMessage",
    "session_id_for",
]

# Base NATS subjects — tenant-scoped at runtime via ``scope_subject`` →
# ``"<tenant>.messaging.inbound"`` / ``"<tenant>.messaging.outbound"``.
MESSAGING_INBOUND = "messaging.inbound"
MESSAGING_OUTBOUND = "messaging.outbound"


class MessageAttachment(BaseModel):
    """A file or media item that arrived alongside an inbound bridge message.

    Deliberately small for the foundation: enough to record that something was attached
    and a provider-scoped handle to fetch it. Promoting these into core attachments
    (ADR-0019) so the agent can read them is a follow-up; for now the inbound consumer
    answers the text and ignores the bytes.
    """

    kind: str = ""  # e.g. "image", "document", "audio", "video"
    url: str = ""  # provider-scoped location / handle to fetch the bytes
    name: str = ""  # original filename, when the provider supplies one
    mime_type: str = ""


class InboundMessage(BaseModel):
    """A normalized message arriving from an external channel (→ :data:`MESSAGING_INBOUND`).

    Provider-agnostic: every bridge maps its native update onto this one shape, so the core
    consumer is identical for Telegram, Discord, Slack, …. ``channel_id`` + the optional
    ``thread_id`` say where a reply must go; ``provider_msg_id`` is the bridge's own id for
    this message (used for reply-threading and de-duplication).
    """

    tenant: str
    bridge: str  # the provider id, e.g. "telegram", "discord", "loopback"
    channel_id: str  # the chat / channel / room the message arrived in
    thread_id: str | None = None  # a sub-thread within the channel, when the provider has one
    sender_id: str = ""  # the provider's id for the author
    sender_name: str = ""  # the author's display name, when known
    text: str = ""  # the message body
    attachments: list[MessageAttachment] = Field(default_factory=list)
    provider_msg_id: str = ""  # the bridge's id for this inbound message

    def session_id(self) -> str:
        """The persisted-conversation key for this message's channel (:func:`session_id_for`)."""
        return session_id_for(self.bridge, self.channel_id, self.thread_id)


class OutboundMessage(BaseModel):
    """A reply the core routes back to a channel (→ :data:`MESSAGING_OUTBOUND`).

    The core fills ``bridge`` / ``channel_id`` / ``thread_id`` straight from the originating
    :class:`InboundMessage`, so the ``messaging`` module delivers the reply to the right
    place without holding any per-turn routing state. ``reply_to_msg_id`` lets a provider
    thread the reply under the user's message when it supports quoting.
    """

    tenant: str
    bridge: str
    channel_id: str
    thread_id: str | None = None
    text: str = ""
    reply_to_msg_id: str | None = None


def session_id_for(bridge: str, channel_id: str, thread_id: str | None = None) -> str:
    """The conversation key for a bridge channel: ``"<bridge>:<channel>[:<thread>]"``.

    Maps an external channel onto a stable ``session_id`` so its turns persist as one
    conversation in ``agent_messages`` (the same keying the web UI uses), and so a thread
    gets its own session while the channel's main timeline gets another. Deterministic: the
    inbound consumer and any tooling derive the same id from the same channel.
    """
    base = f"{bridge}:{channel_id}"
    return f"{base}:{thread_id}" if thread_id else base
