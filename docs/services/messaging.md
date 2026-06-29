# messaging — chat bridges

**`epicurus-messaging`** connects external chat channels (Telegram, Discord, Slack, …) to the
assistant: an inbound message drives an agent turn and the reply routes back to the same
chat. It is the **module half** of the inbox contract (ADR-0058) — the [core](core-app.md)
owns the turn in between. It is **provider-pluggable** (ADR-0016): the active bridge is one
class behind a seam, and the built-in **loopback** bridge gives a working path with no
external account. Host port **8093**.

This module is the gating foundation for Phase 4; the individual bridges (Telegram #365,
Discord #366, Slack #367, WhatsApp #368) fan out after it as new providers.

## What it is

A transport, not an agent capability — so it exposes **no MCP tools** and **never calls an
LLM** (constraint #8). It does two things: publish a normalized
[`InboundMessage`](../reference/messaging.md#inboundmessage) when a bridge receives a message,
and consume [`OutboundMessage`](../reference/messaging.md#outboundmessage) to deliver the
agent's reply. The core's inbound consumer keys each channel to a persisted session and runs
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
| `GET /status` | Live status the shell renders (core-proxied at `/platform/v1/modules/messaging/status`): the active `provider`, `connected`, the two subjects, and (loopback) the `delivered` reply count. |
| `POST /loopback/inject` | **Loopback only** — originate an inbound message as if a user sent it (`{text, channel_id?, thread_id?, tenant?, sender_id?, sender_name?}`). The dev/manual path to exercise inbound → turn → outbound end-to-end with a model running, no external account. **404** for any other provider. |

### The provider seam (ADR-0016)

[`BridgeProvider`](../reference/index.md) is a `Protocol` with `provider_name()`,
`secret_names()`, `connected()`, `start(on_inbound)`, `send(message)`, and `stop()`. Adding a
bridge is one new class — the NATS wiring is unchanged. A provider declares the OpenBao secrets
it needs via `secret_names()` (they flow into the manifest `secrets[]`) and reads its per-tenant
bot token with the shipped helper `bridge_token(secrets, bridge, tenant=...)` (reads
`messaging/<bridge>` → `token`); `connected()` backs the `connected` field on `/status`. The
built-in `LoopbackProvider` needs no secret and is always `connected`.

### The Telegram bridge (ADR-0061)

The first real bridge (#365). Select it with `MESSAGING_PROVIDER=telegram` and store the bot
token in OpenBao at `messaging/telegram` → `token` (per-tenant); the service reads it on startup
and begins long-polling. With **no token stored the bridge is idle** — it logs and does not poll
— so set the token and restart to bring it up (`/status` reports `connected: false` until then).

- **Inbound** — a long poll over `getUpdates` maps each message onto an `InboundMessage`: chat
  id → `channel_id`, a forum topic → `thread_id`, `from` → `sender_id` / `sender_name`,
  `message_id` → `provider_msg_id`. v1 handles **text** messages (a media caption counts as
  text); stickers, edits, and service messages have no usable text and are skipped.
- **Outbound** — `sendMessage` delivers the reply to the same chat/thread, splitting on
  Telegram's 4096-character limit and quoting the user's message on the first chunk when
  `reply_to_msg_id` is set.
- **Replies are plain text** (no `parse_mode`): an agent answer is arbitrary Markdown that is
  not valid Telegram MarkdownV2 without escaping, and one bad entity makes the API reject the
  whole message — so plain text guarantees delivery. Rich formatting is a follow-up.
- **Long polling, not a webhook** — a webhook needs a public HTTPS ingress, which the
  local-first default does not expose (constraint #7); long polling reaches Telegram
  outbound-only. Single-tenant for v1, mirroring the foundation's outbound consumer.

## Configuration

Extends the shared [`CoreSettings`](../reference/config.md) with:

| Env var | Default | Meaning |
| --- | --- | --- |
| `MESSAGING_PROVIDER` | `loopback` | The active bridge. `loopback` is the in-process echo bridge (no external service, no secret); `telegram` is the first real bridge. |
| `OPENBAO_URL` / `OPENBAO_TOKEN` | `http://openbao:8200` / — | Where a real bridge's per-tenant bot token is read from (unused by loopback). |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | Bot API base URL (telegram bridge only); override to point at a proxy or mock. |
| `TELEGRAM_POLL_TIMEOUT` | `30` | `getUpdates` long-poll window in seconds (telegram bridge only). |

Plus the shared `NATS_URL`, `DEFAULT_TENANT_ID`, `LOG_LEVEL`.

## Data model

None of its own — the conversation history lives in the core's `agent_messages`
(tenant-scoped, keyed by the bridge session id). Bridge bot tokens live in OpenBao under
`tenants/<tenant>/messaging/<bridge>`. The loopback provider keeps an in-memory list of
delivered replies (disposable; surfaced as `delivered` in `/status`).

## Dependencies

NATS (both subjects) · OpenBao (per-tenant bridge tokens, for real bridges) · the
[core](core-app.md) runs the turn and publishes the reply. A bridge provider also talks to
its external service (Telegram, …) — never to an LLM (constraint #8).

## Run & extend

Comes up with the default stack on the loopback bridge. Drive it end-to-end (with a model
running) via `POST /loopback/inject`:

```bash
curl -s localhost:8093/loopback/inject -H 'content-type: application/json' \
  -d '{"text":"what is on my calendar today?","channel_id":"demo"}'
# → messaging.inbound → core turn → messaging.outbound → delivered (see GET /status)
```

To add a real bridge (the #365+ pattern, with `telegram_provider.py` as the worked example):
implement `BridgeProvider` in a new `<name>_provider.py` (its `start()` polls/receives and calls
`on_inbound`; its `send()` posts the reply), read the token with `bridge_token`, return its
secret path from `secret_names()`, and add a branch to `build_provider` in `service.py`. The
OpenBao wiring and the NATS contract are already in place. Package `epicurus_messaging`:
`service.py` (module + provider factory), `providers.py` (the seam + `bridge_token`),
`loopback_provider.py`, `telegram_provider.py`, `settings.py`, `app.py`.
