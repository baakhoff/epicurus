# Reference: Hermarchus API & configuration

The controller ↔ Hermarchus contract, `contract_version` **1** (the `/v1` prefix), as
implemented by `services/hermarchus` 0.1.0. What the block is and how to run it:
[hermarchus](../services/hermarchus.md).

## Access

| | |
| --- | --- |
| Auth | `Authorization: Bearer <token>` on every `/v1` route. Missing, wrong, or another scheme → **401** `{"detail":{"code":"unauthenticated",…}}` with `WWW-Authenticate: Bearer`. Compared in constant time. |
| Open routes | `GET /health`, `GET /metrics` — nothing else (no `/docs`, `/redoc`, `/openapi.json`) |
| Profile | Optional on any `/v1` request: `X-Epicurus-Profile`, `X-Epicurus-Tenant`, or `?tenant_id=`. Must equal the bound profile → else **403** `wrong_profile`; malformed (the `epicurus-core` tenant rule: 1–63 of `[a-z0-9-]`, no edge hyphen) → **400** `invalid_profile`. |
| Errors | `{"detail": {"code": "<code>", "message": "<sentence>"}}`; body validation errors are FastAPI's 422. |

## Endpoints

### `GET /v1/info`

```json
{
  "service": "hermarchus", "version": "0.1.0", "contract_version": 1,
  "acp_protocol_version": 1, "profile": "local",
  "limits": {"max_sessions": 10, "live_sessions": 0, "stream_queue_size": 1000,
             "permission_timeout_s": 1800.0, "unattended_timeout_s": 60.0,
             "unattended_default": false},
  "agents": [
    {"kind": "echo", "name": "Echo (reference agent)", "builtin": true, "verified": true,
     "command": "/app/.venv/bin/python", "args": ["-m", "epicurus_hermarchus.echo_agent"],
     "env": [], "install_command": null, "install": null, "docs": [],
     "available": true, "reason": null},
    {"kind": "codex", "name": "Codex", "builtin": false, "verified": false,
     "command": "codex-acp", "args": [], "env": [],
     "install_command": "npm install -g @agentclientprotocol/codex-acp",
     "install": {"state": "failed", "detail": "the install command exited 1",
                 "started_at": "…", "finished_at": "…", "output_tail": ["…"]},
     "docs": ["https://…"], "available": false,
     "reason": "command 'codex-acp' not found on the agent PATH — install it …"}
  ]
}
```

`env` lists a kind's variable **names** only. `install` is `null` unless the kind is in
`HERMARCHUS_INSTALL_KINDS`; `state` is `pending`, `running`, `succeeded`, `failed` or `skipped`.

### Sessions

| Request | Body | Response |
| --- | --- | --- |
| `POST /v1/sessions` | `{"agent": str (1–64), "cwd": str = ".", "title": str? (≤200), "unattended": bool?}` — extra keys refused | **201** session (status `starting`) · 404 `not_found` (unknown kind) · 409 `agent_unavailable` · 400 `invalid_cwd` · 429 `session_limit_reached` |
| `GET /v1/sessions` | — | `{"sessions": [session, …]}` oldest first |
| `GET /v1/sessions/{id}` | — | session · 404 |
| `POST /v1/sessions/{id}/input` | `{"text": str (1–1 000 000), "mode": "queue" \| "steer" = "queue"}` | **202** `{"input_id", "mode", "seq", "status": "pending"}` (`seq` of its `input.admitted`) · 409 `session_not_live` |
| `POST /v1/sessions/{id}/cancel` | — | **202** `{"status": "cancel_requested"}` · 409 `no_turn_running` / `session_not_live` |
| `POST /v1/sessions/{id}/stop` | — | 200 session (`stopped`; a session already ended is returned as is) |
| `DELETE /v1/sessions/{id}` | — | **204** (stops a live session first; deletes its log, inbox and requests; ends its streams) |

`cwd` is relative to the workspace, or absolute inside it. A `..` component, a path outside, or a
path whose symlinks resolve outside is refused **before** anything is created; otherwise the
directory is created with its parents. Session object:

```json
{"id": "9f…32 hex", "profile": "local", "agent": "echo", "cwd": "demo", "title": null,
 "status": "idle", "unattended": false, "agent_session_id": "echo-1", "last_seq": 2,
 "detail": null, "created_at": "2026-11-17T10:00:00.000Z",
 "updated_at": "2026-11-17T10:00:00.400Z", "turn_running": false}
```

### Events — `GET /v1/sessions/{id}/events`

Query: `after` (int ≥ 0, default 0, or the `Last-Event-ID` header when absent), `follow` (bool,
default false). Response `text/event-stream`, `Cache-Control: no-cache`. Frames:
`id: <seq>` / `event: <type>` / `data: <event JSON>`, the event JSON being
`{"session_id", "seq", "type", "at", "data"}`. A following stream sends `: keepalive` every 15 s,
ends after a terminal event or a delete, and — when this connection is more than
`HERMARCHUS_STREAM_QUEUE_SIZE` events behind — ends with a frame `event: stream.overflow` /
`data: {"resume_after": <last seq delivered>}` (no `id`).

### Permissions

| Request | Body | Response |
| --- | --- | --- |
| `GET /v1/sessions/{id}/permissions?pending=true` | — | `{"permissions": [{"id", "session_id", "status": "pending"\|"resolved", "request": {"tool_call", "options"}, "decision", "resolved_by", "created_at", "expires_at", "resolved_at"}]}`; `pending=false` lists all |
| `POST /v1/sessions/{id}/permissions/{request_id}` | `{"decision": "once" \| "always" \| "reject"}` | 200 `{"request_id", "decision", "seq"}` · 404 (unknown, or another session's) · 409 `permission_not_pending` / `session_not_live` |

`once` selects the agent's `allow_once` option only; `always` its `allow_always`, else
`allow_once`; `reject` its `reject_once`, else `reject_always`. With no fitting option the agent is
answered `cancelled`.

`decision` in a resolved request is `once`, `always`, `reject` or `cancelled`; `resolved_by` is
`controller`, `timeout`, `cancel`, `session_failed`, `host_shutdown`, `stopped by controller` or
`host_restart`.

## Events

| Type | `data` |
| --- | --- |
| `session.created` | `{agent, cwd, title, unattended}` |
| `session.ready` | `{protocol_version, agent_info, agent_session_id}` |
| `session.failed` | `{reason, exit_code, stderr: [last lines]}` |
| `session.stopping` | `{reason}` — before the process is signalled |
| `session.stopped` / `session.interrupted` | `{reason, exit_code?}` |
| `input.admitted` / `input.delivered` | `{input_id, mode, text}` |
| `input.dropped` | `{input_id, reason}` |
| `turn.started` | `{input_id}` |
| `turn.cancel_requested` | `{reason: "controller" \| "steer", input_id?}` |
| `turn.ended` | `{input_id, stop_reason, error?}` |
| `agent.message` / `agent.thought` / `agent.user_message` | ACP `ContentChunk`: `{content, messageId?}` |
| `agent.tool_call` / `agent.tool_call_update` | ACP `ToolCall` / `ToolCallUpdate` fields (`toolCallId`, `title`, `kind`, `status`, `content`, `locations`, `rawInput`, `rawOutput`) |
| `agent.plan` | `{entries: [{content, priority, status}]}` |
| `agent.update` | `{kind: <ACP sessionUpdate>, update}` for any other update |
| `permission.requested` | `{request_id, tool_call, options, unattended, expires_at}` |
| `permission.resolved` | `{request_id, decision, by, option_id, remembered?, tool_call?}` (`request_id` is `null` and `by` is `remembered` when a remembered rule answered without asking) |
| `file.read` | `{path}` (workspace-relative) |
| `file.write` | `{path, bytes}` — before the write |
| `file.write_failed` | `{path, error}` |

## Configuration

Environment, prefix `HERMARCHUS_` (`LOG_LEVEL`, `APP_ENV`, `JSON_LOGS` unprefixed, as stack-wide).
The service **refuses to start** (exit 2, the reason on stderr) on a missing or short token, an
invalid profile, a pass-through naming a `HERMARCHUS_*` variable, a state directory or agent
home that overlaps the workspace, or an invalid kinds file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMARCHUS_TOKEN` | — | bearer token, ≥ 16 characters (required unless `_TOKEN_FILE`) |
| `HERMARCHUS_TOKEN_FILE` | — | file holding the token (a mounted secret) |
| `HERMARCHUS_PROFILE` | `local` | the bound profile (tenant id) |
| `HERMARCHUS_PORT` | `8080` | listen port inside the container |
| `HERMARCHUS_WORKSPACE_DIR` | `/workspace` | agents' workspace; every `cwd` and file path resolves inside |
| `HERMARCHUS_STATE_DIR` | `/state` | holds `hermarchus.sqlite3` |
| `HERMARCHUS_AGENT_HOME` | `/home/hermarchus` | agents' `HOME` |
| `HERMARCHUS_MAX_SESSIONS` | `10` | live sessions (`starting`/`idle`/`running`) at once |
| `HERMARCHUS_STREAM_QUEUE_SIZE` | `1000` | per-connection event buffer |
| `HERMARCHUS_UNATTENDED` | `false` | default `unattended` for new sessions |
| `HERMARCHUS_UNATTENDED_TIMEOUT_S` | `60` | unattended: unanswered permission → reject |
| `HERMARCHUS_PERMISSION_TIMEOUT_S` | `1800` | attended: unanswered permission → reject |
| `HERMARCHUS_AGENT_START_TIMEOUT_S` | `120` | `initialize` + `session/new` deadline |
| `HERMARCHUS_STOP_GRACE_S` | `5` | SIGTERM → SIGKILL grace when stopping an agent |
| `HERMARCHUS_KINDS_FILE` | — | YAML: `kinds: {<name>: {name, command, args, env, install, docs, enabled}}` |
| `HERMARCHUS_INSTALL_KINDS` | — | comma-separated kinds to install at start |
| `HERMARCHUS_INSTALL_TIMEOUT_S` | `900` | per install command |
| `HERMARCHUS_CHILD_ENV_PASSTHROUGH` | — | comma-separated extra variable names agents inherit |

**Compose overlay** (`services/hermarchus/compose.yaml`) passes `HERMARCHUS_TOKEN` (required —
`compose` itself refuses to resolve without it), `HERMARCHUS_PROFILE` (`local`),
`HERMARCHUS_MAX_SESSIONS` (`10`), `HERMARCHUS_UNATTENDED` (`false`), `HERMARCHUS_INSTALL_KINDS`,
`LOG_LEVEL`, publishes `${BIND_ADDRESS:-127.0.0.1}:${HERMARCHUS_PORT:-8094}` (a *host* port —
the overlay does not pass `HERMARCHUS_PORT` into the container, which listens on 8080), and
attaches the service to its own `hermarchus` network, not the stack's `epicurus`.

**Chart** — the `hermarchus:` values block, see
[Kubernetes](../infrastructure/kubernetes.md#hermarchus--the-agent-host).

## Metrics

`hermarchus_sessions_live` (gauge), `hermarchus_sessions_created_total{agent}`,
`hermarchus_events_total{type}`, `hermarchus_stream_overflows_total`,
`hermarchus_permission_resolutions_total{decision,by}`. No label carries an id, path, title or
prompt.
