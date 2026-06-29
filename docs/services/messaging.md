# messaging — chat bridges

**`epicurus-messaging`** connects external chat channels (Discord, Telegram, Slack, …) to the
assistant: an inbound message drives an agent turn and the reply routes back to the same
chat. It is the **module half** of the inbox contract (ADR-0058) — the [core](core-app.md)
owns the turn in between. It is **provider-pluggable** (ADR-0016) and runs **every bridge at
once** (ADR-0062): the built-in **loopback** echo bridge is always on, and each real bridge is
dormant until the operator connects it with a bot token. Host port **8093**.

This module is the Phase-4 foundation; the bridges fan out as providers — **Discord (#366)**
has landed, with Telegram (#365), Slack (#367) and WhatsApp (#368) to follow. Connect and
manage them from the web shell's **Settings → Chat bridges** surface (#369).

## What it is

A transport, not an agent capability — so it exposes **no MCP tools** and **never calls an
LLM** (constraint #8). It does two things: publish a normalized
[`InboundMessage`](../reference/messaging.md#inboundmessage) when a bridge receives a message,
and consume [`OutboundMessage`](../reference/messaging.md#outboundmessage) to deliver the
agent's reply — **dispatched to the bridge named by the message's `bridge` field**, so several
bridges coexist. The core's inbound consumer keys each channel to a persisted session and runs
a headless turn, so memory/facts stay tenant-scoped — one brain across the web UI and every
bridge.

## The contract it exposes

### Events (NATS) — the inbox contract (ADR-0058)

| Direction | Subject | Shape |
| --- | --- | --- |
| **Emits** | `<tenant>.messaging.inbound` | [`InboundMessage`](../reference/messaging.md#inboundmessage) — a message received from a channel. |
| **Consumes** | `<tenant>.messaging.outbound` | [`OutboundMessage`](../reference/messaging.md#outboundmessage) — an agent reply to deliver. |

### HTTP

`GET /health` · `GET /metrics` · `GET /manifest` · `/mcp` (the streamable MCP endpoint, with
no tools) plus:

| Endpoint | Purpose |
| --- | --- |
| `GET /status` | Live status the shell renders (core-proxied at `/platform/v1/modules/messaging/status`): the two subjects and a `bridges[]` list, each a [`BridgeStatus`](../reference/messaging.md#bridgestatus) (`configured` / `enabled` / `connected` / `detail`). |
| `POST /bridges/{bridge}/reload` | The core's control path (#369): re-read a bridge's token from OpenBao and (re)connect or disconnect it at runtime — no restart. Returns the bridge's fresh `BridgeStatus`; **404** for an unknown bridge. |
| `POST /loopback/inject` | **Loopback only** — originate an inbound message as if a user sent it (`{text, channel_id?, thread_id?, tenant?, sender_id?, sender_name?}`). The dev/manual path to exercise inbound → turn → outbound end-to-end with a model running, no external account. |

### The provider seam (ADR-0016 / ADR-0062)

[`BridgeProvider`](../reference/index.md) is a `Protocol` with `provider_name()`,
`secret_names()`, `start(on_inbound)`, `send(message)`, `stop()`, and `status()`. Adding a
bridge is one new class — the NATS wiring is unchanged. The `BridgeManager` holds every
provider, starts them all, dispatches each outbound reply to the one whose `provider_name()`
matches the message's `bridge`, and reloads a single bridge on the core's control call. A
provider declares the OpenBao secrets it needs via `secret_names()` (they flow into the manifest
`secrets[]`) and reads its per-tenant token + on/off flag with the shipped helper
`load_bridge_secret(secrets, bridge, tenant=...)` (reads `messaging/<bridge>` →
`{token, enabled}`; `bridge_token(...)` is the token-only convenience). The built-in
`LoopbackProvider` needs no secret.

## Configuration

Extends the shared [`CoreSettings`](../reference/config.md) with:

| Env var | Default | Meaning |
| --- | --- | --- |
| `MESSAGING_PROVIDER` | `loopback` | **Deprecated / no-op** (ADR-0062). Bridge selection is no longer a single env choice — every bridge runs at once and a real bridge connects when its token is stored. Kept harmless for backward compatibility. |
| `OPENBAO_URL` / `OPENBAO_TOKEN` | `http://openbao:8200` / — | Where a real bridge's per-tenant bot token is read from (unused by loopback). |

Plus the shared `NATS_URL`, `DEFAULT_TENANT_ID`, `LOG_LEVEL`.

## Data model

None of its own — the conversation history lives in the core's `agent_messages`
(tenant-scoped, keyed by the bridge session id). Bridge bot tokens live in OpenBao under
`tenants/<tenant>/messaging/<bridge>` as `{token, enabled}` — written by the core's bridge admin
(#369), read by the provider. The loopback provider keeps an in-memory list of delivered
replies (disposable; surfaced in its `BridgeStatus.detail`).

## Dependencies

NATS (both subjects) · OpenBao (per-tenant bridge tokens, for real bridges) · the
[core](core-app.md) runs the turn and publishes the reply, and owns the connect/manage surface
(#369). A bridge provider also talks to its external service — Discord via `discord.py`
(gateway in, REST out) — never to an LLM (constraint #8).

## Run & extend

Comes up with the default stack; the loopback bridge is always on. Drive the loop end-to-end
(with a model running) via `POST /loopback/inject`:

```bash
curl -s localhost:8093/loopback/inject -H 'content-type: application/json' \
  -d '{"text":"what is on my calendar today?","channel_id":"demo"}'
# → messaging.inbound → core turn → messaging.outbound → delivered (see GET /status)
```

### Connect Discord (#366)

1. Create an application + bot in the Discord Developer Portal; copy the **bot token**.
2. Under **Bot → Privileged Gateway Intents**, enable the **Message Content Intent** (required
   to read message text).
3. Invite the bot with the `bot` scope and the **View Channels**, **Send Messages**, and
   **Read Message History** permissions.
4. In the web shell, **Settings → Chat bridges → Discord → Connect**, and paste the token. The
   core stores it in OpenBao and reloads the bridge; it connects within a few seconds.

The bot answers **direct messages**, and in a server only messages that **@mention** it; it
never answers itself. Replies are thread-aware and chunked to Discord's 2000-char limit.

### Add a bridge (the #365+ pattern)

Implement `BridgeProvider` in a new `<name>_provider.py` (its `start()` reads the token via
`load_bridge_secret` and connects, calling `on_inbound` per message; its `send()` posts the
reply; `status()` reports a `BridgeStatus`), return its secret path from `secret_names()`, and
add it to `build_bridges` in `service.py`. The OpenBao wiring, the multi-bridge dispatch, and
the connect/manage surface are already in place — no core change needed.

Package `epicurus_messaging`: `service.py` (module + `build_bridges`), `manager.py` (the
multi-bridge `BridgeManager`), `providers.py` (the seam + `BridgeStatus` + `load_bridge_secret`),
`discord_provider.py`, `loopback_provider.py`, `settings.py`, `app.py`.
