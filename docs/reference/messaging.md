# Reference: `messaging`

`epicurus_core.messaging` — the normalized **inbox contract** for the chat-bridge path
(ADR-0058): the two NATS subjects and the Pydantic shapes the `messaging` module and the core
exchange so an external channel (Telegram, Discord, …) can drive the agent.

The flow is: a bridge receives a message → the `messaging` module publishes an
[`InboundMessage`](#inboundmessage) on `messaging.inbound` → the core runs a headless agent
turn → the core publishes an [`OutboundMessage`](#outboundmessage) on `messaging.outbound` →
the `messaging` module delivers the reply via the active bridge. Both subjects are
tenant-scoped via [`scope_subject`](tenancy.md) and `tenant` is a field on both shapes
(constraint #1); the contract travels the internal Docker network only (constraint #7).

Everything below is importable from the top level, e.g.
`from epicurus_core import InboundMessage, MESSAGING_INBOUND`.

## Subjects

| Constant | Value | Direction |
| --- | --- | --- |
| `MESSAGING_INBOUND` | `messaging.inbound` | module → core (a message arrived from a channel) |
| `MESSAGING_OUTBOUND` | `messaging.outbound` | core → module (an agent reply to deliver) |

Scoped at runtime: `scope_subject(MESSAGING_INBOUND, "local")` → `local.messaging.inbound`.

## `InboundMessage`

A normalized message arriving from an external channel. Every bridge maps its native update
onto this one shape, so the core consumer is identical for every provider.

| Field | Type | Meaning |
| --- | --- | --- |
| `tenant` | `str` | Owning tenant (required; scopes the turn + the reply). |
| `bridge` | `str` | Provider id, e.g. `telegram`, `discord`, `loopback`. |
| `channel_id` | `str` | Chat / channel / room the message arrived in (where a reply goes). |
| `thread_id` | `str \| None` | Sub-thread within the channel, when the provider has one. |
| `sender_id` | `str` | Provider's id for the author. |
| `sender_name` | `str` | Author's display name, when known. |
| `text` | `str` | The message body. |
| `attachments` | `list[MessageAttachment]` | Files/media that arrived with the message. |
| `provider_msg_id` | `str` | The bridge's id for this message (reply-threading / dedup). |

**`session_id() -> str`** — the persisted-conversation key for this message's channel; a thin
wrapper over [`session_id_for`](#session_id_for).

## `OutboundMessage`

A reply the core routes back to a channel. The core fills `bridge` / `channel_id` /
`thread_id` from the originating `InboundMessage`, so the module needs no per-turn routing
state.

| Field | Type | Meaning |
| --- | --- | --- |
| `tenant` | `str` | Owning tenant (required). |
| `bridge` | `str` | Provider to deliver through. |
| `channel_id` | `str` | Channel to deliver to. |
| `thread_id` | `str \| None` | Sub-thread, when the original had one. |
| `text` | `str` | The agent's reply. |
| `reply_to_msg_id` | `str \| None` | Quote/thread the reply under the user's message, when supported. |

## `MessageAttachment`

A file/media item that arrived with an inbound message: `kind`, `url` (a provider-scoped
handle), `name`, `mime_type` — all optional. Deliberately minimal for the foundation;
promoting these into core attachments (ADR-0019) so the agent can read them is a follow-up.

## `session_id_for`

```python
def session_id_for(bridge: str, channel_id: str, thread_id: str | None = None) -> str
```

The conversation key for a bridge channel: `"<bridge>:<channel>[:<thread>]"`. Maps an
external channel onto a stable `session_id`, so its turns persist as one conversation in
`agent_messages` (the same keying the web UI uses) — a thread gets its own session, the
channel's main timeline another. Deterministic: the same channel always yields the same id.

### Example

```python
from epicurus_core import EventBus, InboundMessage, MESSAGING_INBOUND

async with EventBus(settings.nats_url) as bus:
    msg = InboundMessage(
        tenant="local", bridge="telegram", channel_id="4242", text="what's on my calendar?"
    )
    await bus.publish(MESSAGING_INBOUND, msg.model_dump(), tenant_id=msg.tenant)
    # → the core runs a turn keyed by msg.session_id() == "telegram:4242"
    #   and publishes the reply on messaging.outbound for the bridge to deliver.
```

## Bridge management (ADR-0062)

The `messaging` module runs **every bridge at once** — the always-on in-process `loopback` echo
plus each real bridge (Discord, …), which stays dormant until its per-tenant bot token is
stored. An outbound reply is dispatched to the bridge named by `OutboundMessage.bridge`.

### `BridgeStatus`

One bridge's live state — reported by the module's `GET /status` (`bridges[]`) and surfaced to
the operator by the core (#369).

| Field | Type | Meaning |
| --- | --- | --- |
| `bridge` | `str` | Provider id (`discord`, `loopback`, …). |
| `label` | `str` | Human name for the shell. |
| `manageable` | `bool` | Operator connects/disconnects it (false for `loopback`). |
| `configured` | `bool` | A bot token is stored in OpenBao. |
| `enabled` | `bool` | The operator's on/off (kept across reconnects). |
| `connected` | `bool` | The live link to the external service is up right now. |
| `detail` | `str` | Short human summary (`2 server(s) · 5 channel(s)`, `no bot token set`, …). |

### Module HTTP (internal, core → module)

| Endpoint | Purpose |
| --- | --- |
| `GET /status` | `{inbound_subject, outbound_subject, bridges: [BridgeStatus, …]}`. |
| `POST /bridges/{bridge}/reload` | The core's control path: re-read the bridge's token and (re)connect or disconnect it at runtime. Returns the bridge's fresh `BridgeStatus`. **404** unknown bridge. |

### Core HTTP — bridge admin (`/platform/v1/messaging`, #369)

The browser never holds a token (constraint #6); the core writes it to OpenBao
(`messaging/<bridge>` → `{token, enabled}`), then calls the module's reload so the bridge
connects at runtime — no restart.

| Endpoint | Purpose |
| --- | --- |
| `GET /platform/v1/messaging/bridges` | List every bridge + its `BridgeStatus`. |
| `PUT /platform/v1/messaging/bridges/{bridge}/token` | Connect: store the write-only bot token (and reload). Body `{token}`. |
| `POST /platform/v1/messaging/bridges/{bridge}/enabled` | On/off without forgetting the token. Body `{enabled}`. |
| `DELETE /platform/v1/messaging/bridges/{bridge}` | Disconnect: clear the token (and reload). |

### Discord bridge (#366)

The `discord` provider maintains the Discord **gateway** (WebSocket, via `discord.py`) for
inbound and posts replies with the REST API (thread-aware, chunked to Discord's 2000-char
limit). It **ignores its own messages**, and in a shared server treats a message as a turn only
when the **bot is @mentioned** (a DM is always a turn). Reading message text requires the
privileged **Message Content Intent**; see [messaging](../services/messaging.md) for the bot
setup (intents, invite scopes and permissions).

See the running services that speak this contract: [messaging](../services/messaging.md)
(the module side) and [core-app](../services/core-app.md#events-nats) (the inbound consumer).
