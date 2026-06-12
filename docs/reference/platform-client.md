# Reference: `PlatformClient`

`epicurus_core.PlatformClient` — the typed client a **module** uses to call the core's
[platform API](platform-api.md) (module → core, ADR-0004). A module imports it from
`epicurus_core` and requests **inference** without holding any provider SDK or API key:
the core's LLM gateway (ADR-0010) owns model selection, key management, fallback, and
usage accounting. This is how "all AI goes through the core" is enforced in practice.

## Construct

```python
from epicurus_core import PlatformClient

client = PlatformClient(
    base_url=settings.platform_url,   # internal URL of the core, e.g. http://core-app:8080
    tenant_id=settings.default_tenant_id,
)
```

The client is stateless and cheap; make one per module, scoped to its tenant. Every
request carries that tenant, so usage is metered and resources are scoped correctly.

## Methods

### `await client.embed(texts, *, model=None) -> list[list[float]]`

Embed `texts` via the core (`POST /platform/v1/embed`). Returns one float vector per
input. When `model` is omitted the core uses its configured embedding model. Used by the
knowledge module to index a vault.

```python
vectors = await client.embed(["text to index", "another"])
```

### `await client.chat(messages, *, model=None, tools=None) -> PlatformChatResponse`

A chat completion via the core (`POST /platform/v1/chat`). The module supplies only
messages (`PlatformMessage`); the core picks the model, applies fallbacks, and meters
usage. `PlatformChatResponse` carries `model`, `content`, optional `tool_calls`, and token
counts.

```python
from epicurus_core import PlatformMessage

reply = await client.chat([PlatformMessage(role="user", content="summarise this")])
```

## Errors

Both methods raise `httpx.HTTPStatusError` on a non-2xx response — notably **`503`** when
the gateway is paused (ADR-0005). A module should treat inference as best-effort and
degrade gracefully.

## Why a client (and not the provider SDK)

- **Keys stay in the core** (OpenBao) — a module never sees a model credential, so a
  compromised or community module cannot exfiltrate one.
- **Local ↔ hosted is transparent** — the module's code is identical whether the core
  routes to Ollama or a hosted provider.
- **One metering point** — every call emits a tenant-scoped `llm.usage` event for
  observability and SaaS billing.

See also: [platform-api](platform-api.md) (the wire endpoints) and
[core-app](../services/core-app.md) (the server side).
