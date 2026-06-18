# Mail module

## What it is

The **mail module** gives the agent the ability to search, read, and send mail on
the user's behalf.  The module is **provider-agnostic**: tools are named and typed
in terms of the mail domain (`mail_search`, `mail_read`, `mail_send`), and the
underlying provider is pluggable via the `MailProvider` interface (ADR-0016).

Gmail is the v0.1 provider.  Tokens are fetched at runtime from the core's OAuth
vault — the module never holds a client secret or refresh token (see
[OAuth reference](../reference/oauth.md)).  Future providers (IMAP/SMTP,
Microsoft) will implement the same interface without changing the tools.

**v0.2.0** (Phase 3.8): `mail_search` results now surface as entity-reference chips
in the chat UI (ADR-0019).  Hover shows a compact preview; clicking opens the full
message in the right-panel `email-reader` view (ADR-0018), read-only.  The module
now declares `resolver: true` and serves `GET /resolve/message/{ref_id}` (hover-card)
and `GET /messages/{ref_id}` (full email for the panel).

**v0.4.0** (Phase 3.8): the hover-card resolver now reports the message's **unread**
status.  When a referenced message is unread, the `HoverCard` leads its detail rows
with `Status: Unread`; read messages omit the row rather than render a redundant
"Read".  The provider-agnostic `MailMessage` gains an `unread` flag the Gmail
provider derives from the `UNREAD` label.  The chip's click still opens the
read-only `email-reader` panel directly, so the resolver carries no `href` (there is
no outbound URL — the reader is in-app panel navigation).  Mail skips a 0.3.0
"attach" step because it is read-only, jumping 0.2.0 → 0.4.0.

---

## Contract

### MCP tools

All three tools operate on the active `MailProvider` for the tenant.

| Tool | Inputs | Output | Notes |
| --- | --- | --- | --- |
| `mail_search` | `query: str`, `max_results: int = 10` | `ToolEnvelope` (text + entity refs) | Returns entity-ref chips; no body. Gmail query syntax. Max 50. |
| `mail_read` | `message_id: str` | `str` (formatted text) | Subject, sender, date, and decoded plain-text body for the agent to reason on. |
| `mail_send` | `to: str`, `subject: str`, `body: str` | `str` | **Danger action** — sends a real message. Returns `"sent:<id>"`. |

`mail_search` returns a `ToolEnvelope` (ADR-0019): the `text` field is a human-readable
summary; `entity_refs` carries one `EntityRef` per message (`module="mail"`,
`kind="message"`) so the UI renders interactive chips — no body content is
transferred to the model context.

`mail_send` is declared a **danger action** (ADR-0007): the web shell renders a
confirmation prompt before invoking it and the tool docstring requires explicit
user confirmation before it is called.

### HTTP endpoints (internal)

The module serves these HTTP endpoints on the internal Docker network; the core
proxies them to the web shell (the shell never calls the module directly).

| Method | Path | Shape | Purpose |
| --- | --- | --- | --- |
| `GET` | `/resolve/message/{ref_id}` | `HoverCard` | Hover-card resolver (ADR-0019). Returns subject, snippet, sender, recipients, date, and unread status (a `Status: Unread` row, only when unread). No `href` — the chip's click opens the reader. |
| `GET` | `/messages/{ref_id}` | `EmailMessage` | Full email for the panel's `email-reader` view. Returns subject, from, date, body. |
| `GET` | `/status` | `{"gmail_connected": bool}` | Whether a Google token is available — a fast token-presence check (`is_available`), **not** a live Gmail API call (#209), so the polled status panel can't stall the core's status proxy into a Bad Gateway. Proxied by the core. |

The core exposes these via:

```
GET /platform/v1/modules/mail/resolve/message/{ref_id}   → HoverCard
GET /platform/v1/modules/mail/messages/{ref_id}          → EmailMessage
GET /platform/v1/modules/mail/status                     → status JSON
```

#### `HoverCard` shape (from resolver)

The `Status: Unread` row leads the details and is present **only when the message is
unread**; a read message omits it. There is no `href` — clicking the chip opens the
read-only `email-reader` panel directly (in-app navigation, not an outbound URL).

```json
{
  "title": "Invoice from Acme",
  "description": "Please find attached…",
  "details": [
    { "label": "Status", "value": "Unread" },
    { "label": "From",  "value": "acme@example.com" },
    { "label": "To",    "value": "me@example.com" },
    { "label": "Date",  "value": "Mon, 1 Jan 2024 10:00:00 +0000" }
  ]
}
```

#### `EmailMessage` shape (from `/messages/{ref_id}`)

```json
{
  "subject": "Invoice from Acme",
  "from": "acme@example.com",
  "date": "Mon, 1 Jan 2024 10:00:00 +0000",
  "body": "Dear customer,\n\nPlease find the invoice attached.\n\nRegards"
}
```

### NATS events

| Subject (base) | Direction | Payload | Condition |
| --- | --- | --- | --- |
| `mail.sent` | emitted | `{}` | After `mail_send` succeeds |

Subjects are tenant-scoped at runtime: `<tenant_id>.mail.sent`.

---

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `PLATFORM_URL` | `http://localhost:8080` | Internal core base URL. On the Docker network: `http://core-app:8080`. |
| `DEFAULT_TENANT_ID` | `local` | Tenant this module acts on behalf of. |
| `NATS_URL` | `nats://localhost:4222` | NATS event backbone. |
| `LOG_LEVEL` | `info` | Logging verbosity. |
| `BIND_ADDRESS` | `127.0.0.1` | Host-side bind address for the published port. |
| `MAIL_PORT` | `8087` | Host-side port mapped to the container's 8080. |

No secrets are stored in environment variables — the access token is fetched at
call time from the core's OAuth vault.

---

## Data model

The mail module holds **no persistent state**.  It is a pure pass-through to the
provider API.  The access token is managed by the core's OpenBao vault
(`oauth/tokens/google` — see [OAuth reference](../reference/oauth.md)).

---

## Dependencies

| Service | Purpose |
| --- | --- |
| `core-app` | Platform API — token retrieval, event bus |
| `nats` | Event publication (`mail.sent`) |
| Gmail API (`gmail.googleapis.com`) | The underlying mail provider |

---

## Run & extend

### Enable the module

The mail fragment is already included in the top-level `compose.yaml`.  Bring
it up with the rest of the stack:

```bash
task up          # or: docker compose up -d
```

### Connect your Google account

Before the module can access Gmail the operator must:

1. Connect a Google account **with Gmail scopes**.  The default Google OAuth
   connect flow requests only `openid email profile`.  Pass the Gmail scopes
   explicitly:

   ```
   GET /platform/v1/oauth/google/connect
     ?scope=openid%20email%20profile%20https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly%20https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.send
   ```

   The exact scopes are also available as `GMAIL_REQUIRED_SCOPE` in
   `epicurus_mail.gmail`.

2. Ensure the Gmail API is enabled in the Google Cloud project associated with
   the OAuth client credentials (see [OAuth operator setup](../reference/oauth.md#operator-setup-google)).

### Adding a provider

1. Implement `MailProvider` in a new file (e.g. `imap.py`).
2. Add a settings field to select the provider (e.g. `MAIL_PROVIDER=imap`).
3. Instantiate the correct provider in `app.py` and pass it to `build_module`.
4. No changes to `service.py` or the tool surface are needed.

### Run locally (outside Docker)

```bash
# Start the data plane
task infra-up

# Run the mail service with dev defaults
uv run uvicorn epicurus_mail.app:app --reload --port 8087
```

The service will fail health-checks for Gmail tools until a Google account is
connected, but the HTTP surface (`/health`, `/manifest`, `/status`, `/resolve/…`,
`/messages/…`) works immediately.
