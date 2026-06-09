# Reference: `events`

`epicurus_core.events` — the async NATS client (the event backbone). Subjects are
tenant-scoped via [`scope_subject`](tenancy.md).

## `EventBus`

```python
class EventBus:
    def __init__(self, url: str = "nats://localhost:4222") -> None
    @classmethod
    def from_settings(cls, settings: CoreSettings) -> EventBus
```

Use it as an async context manager (`async with EventBus(...) as bus:`) or call
`connect()` / `close()` yourself.

### Methods

| Method | Description |
| --- | --- |
| `async connect() -> None` | Connect to NATS. |
| `async close() -> None` | Drain and disconnect. |
| `async publish(subject, data, tenant_id=None) -> None` | Publish `data` to the tenant-scoped `subject`. |
| `async request(subject, data, *, timeout=2.0, tenant_id=None) -> Event` | Request/reply; await one response. |
| `async subscribe(subject, handler, *, tenant_id=None, queue="") -> Subscription` | Call `handler(Event)` per message. |
| `async reply(subject, replier, *, tenant_id=None, queue="") -> Subscription` | Respond to each request with `replier(Event)`'s result. |
| `client` *(property)* | The underlying NATS client; raises if not connected. |

`data` may be `bytes`, `str`, or a JSON-serializable `dict`. A non-empty `queue`
joins a queue group for load-balanced delivery.

### Types

```python
Payload = bytes | str | dict[str, Any]
EventHandler = Callable[[Event], Awaitable[None]]
Replier = Callable[[Event], Awaitable[Payload]]
```

## `Event`

A received message (frozen dataclass):

| Member | Type | Description |
| --- | --- | --- |
| `subject` | `str` | The fully-scoped subject. |
| `data` | `bytes` | The raw payload. |
| `text` *(property)* | `str` | `data` decoded as UTF-8. |
| `json()` | `Any` | `data` parsed as JSON. |

### Example

```python
from epicurus_core import Event, EventBus

async def on_msg(event: Event) -> None:
    print(event.subject, event.json())

async with EventBus(settings.nats_url) as bus:
    await bus.subscribe("inbox.message", on_msg, tenant_id="local")
    await bus.publish("inbox.message", {"text": "hi"}, tenant_id="local")
```
