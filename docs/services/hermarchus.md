# hermarchus — the agent host

**Hermarchus** runs agent sessions inside its own container (or machine) and exposes them to a
*controller* over HTTP. One instance is bound to exactly **one profile** (a `tenant_id`) and runs
any number of sessions side by side on **one workspace filesystem**. Agents are programs that
speak the [Agent Client Protocol](https://agentclientprotocol.com) (ACP) over stdio — Claude Code,
Codex and Grok Build through their ACP entry points, or the built-in **`echo`** reference agent —
started one process per session. Whoever holds the instance's bearer token is its controller: it
creates sessions, sends input through a durable **inbox**, reads each session's ordered **event
log** as server-sent events with replay, and answers the **permission requests** agents raise.

It is the third part of the split in #1014 (Epicurus the client, Pamphilus the server, Hermarchus
the agent host). In v0 (#1016) nothing in the stack drives it yet: the controller is anything with
the token — curl, the tests, the smoke gates. It **owns nothing** in the stack and uses none of
it: no NATS, no Postgres, no OpenBao, no core. Because its agents run arbitrary code, it is also
kept from *reaching* the stack — a network of its own on Compose, an egress policy on Kubernetes
when `networkPolicy` is on (see [Security model](#security-model--where-the-walls-are)).

It is deliberately a **stateful sandbox** — its filesystem *is* the agents' workspace, and agent
programs and their sign-ins live in its home directory — which is a stated exception to the
"services are stateless" rule. Its own database is a **spool**: it keeps sessions alive across a
controller that goes away; the lasting record of a chat will belong to the controller.

| | |
| --- | --- |
| Package | `services/hermarchus` (`epicurus_hermarchus`), version `0.1.0` |
| Contract | `/v1`, `contract_version` **1**; ACP protocol version **1** |
| Host port | `8094` (`HERMARCHUS_PORT`), loopback — [ports](../reference/ports.md) |
| Platforms | Compose: opt-in overlay `services/hermarchus/compose.yaml`. Kubernetes: chart block `hermarchus.enabled` (off by default) |
| Full reference | [Hermarchus API & configuration](../reference/hermarchus.md) |

## The contract

All of `/v1` requires `Authorization: Bearer <HERMARCHUS_TOKEN>` (401 `unauthenticated`
otherwise). `GET /health` and `GET /metrics` are open, as on every service in the stack; neither
carries a session id, a title, a path or a prompt. Nothing else is served without the token —
there is no `/docs`, `/redoc` or `/openapi.json`. A request **may** name a profile —
`X-Epicurus-Profile` or `X-Epicurus-Tenant` header, or `?tenant_id=` — and if it does, it must be
the bound one: another profile is **403 `wrong_profile`**, a malformed one **400
`invalid_profile`**. Errors are `{"detail": {"code": "...", "message": "..."}}`.

| Method & path | Does | Answers |
| --- | --- | --- |
| `GET /health` | liveness | `{"status":"ok","service":"hermarchus","version":"0.1.0"}` |
| `GET /metrics` | Prometheus | text |
| `GET /v1/info` | version, contract version, profile, limits, every agent kind with `available` and why not | 200 |
| `POST /v1/sessions` | `{"agent", "cwd"=".", "title"?, "unattended"?}` — start a session | 201 session · 404 unknown kind · 409 `agent_unavailable` · 400 `invalid_cwd` · 429 `session_limit_reached` |
| `GET /v1/sessions` | `{"sessions": [session…]}` | 200 |
| `GET /v1/sessions/{id}` | one session | 200 · 404 |
| `POST /v1/sessions/{id}/input` | `{"text", "mode": "queue"\|"steer"}` — admit input to the inbox | 202 `{"input_id","mode","seq","status":"pending"}` · 409 `session_not_live` |
| `POST /v1/sessions/{id}/cancel` | cancel the running turn | 202 · 409 `no_turn_running` |
| `POST /v1/sessions/{id}/stop` | terminate the agent; the session stays readable | 200 session |
| `DELETE /v1/sessions/{id}` | stop if live, then delete the session and its log | 204 |
| `GET /v1/sessions/{id}/events?after=&follow=` | the event log as SSE | 200 `text/event-stream` |
| `GET /v1/sessions/{id}/permissions?pending=true` | permission requests | 200 `{"permissions": [...]}` |
| `POST /v1/sessions/{id}/permissions/{request_id}` | `{"decision": "once"\|"always"\|"reject"}` | 200 `{"request_id","decision","seq"}` · 404 · 409 `permission_not_pending` |

A **session** is `{id, profile, agent, cwd, title, status, unattended, agent_session_id, last_seq,
detail, created_at, updated_at, turn_running}`. `status` is `starting` → `idle` ⇄ `running`, and
ends `stopped`, `failed` (the agent could not start, or exited on its own — `detail` says why) or
`interrupted` (the host stopped under it).

**The inbox.** Input is written durably and recorded (`input.admitted`) before the 202. It is
delivered as one ACP turn (`session/prompt`) at a time: `queue` waits for the running turn to
end; `steer` is delivered **at the next step boundary** — ACP has no way to add to a running
turn, so Hermarchus cancels it (`turn.cancel_requested` with `reason: "steer"`; the agent stops at
its next safe point and answers `cancelled`) and sends the steer as the next prompt, ahead of
anything queued. Input sent while a session is still `starting` is delivered once it is ready.

**The event log.** Every session has an ordered log; `seq` starts at 1 and is gap-free.
`?after=N` replays everything after `N` (`Last-Event-ID` works too); `follow=true` then keeps the
stream open for new events, with a `: keepalive` comment every 15 s, and ends after a terminal
event (`session.stopped`, `session.failed`, `session.interrupted`). Each frame is

```text
id: 7
event: agent.message
data: {"session_id":"…","seq":7,"type":"agent.message","at":"2026-11-17T10:00:00.123Z","data":{…}}
```

Each following connection has a **bounded queue** (`HERMARCHUS_STREAM_QUEUE_SIZE`). A reader that
falls that far behind is dropped — never the session, never the other readers — with a final
`event: stream.overflow` / `data: {"resume_after": N}`; reconnect with `?after=N`.

Event types: `session.created`, `session.ready`, `session.failed`, `session.stopping`,
`session.stopped`, `session.interrupted`, `input.admitted`, `input.delivered`, `input.dropped`,
`turn.started`, `turn.cancel_requested`, `turn.ended` (`stop_reason`: ACP's `end_turn`,
`cancelled`, `max_tokens`, `max_turn_requests`, `refusal`, or `error` with an `error` message),
`agent.message`, `agent.thought`, `agent.user_message`, `agent.tool_call`,
`agent.tool_call_update`, `agent.plan`, `agent.update` (any other ACP session update, verbatim),
`permission.requested`, `permission.resolved`, `file.read`, `file.write`, `file.write_failed`.
The data of each is in the [reference](../reference/hermarchus.md#events).

**Recorded before the side effect.** An event is made durable before it is streamed, and —
where Hermarchus performs the side effect — before the side effect: `input.delivered` before the
prompt is sent, `permission.resolved` before the agent gets its answer, `file.write` before the
bytes are written (`file.write_failed` follows if the write then fails), `session.stopping` /
`session.interrupted` before the process is signalled. **Where that is not possible:** the
`agent.*` events describe things the agent program already did (text it produced, a tool it ran on
its own) — Hermarchus learns of them afterwards, so they are recorded after the fact, and a crash
can lose the last few.

**Permission requests.** An agent's `session/request_permission` becomes `permission.requested`
(`request_id`, the ACP `tool_call`, its `options`, `expires_at`). The controller answers:
`once` picks the agent's `allow_once` option (never its `allow_always`, which the agent program
might remember for good), `always` its `allow_always` option — or `allow_once` if it offers no
other — **and remembers it** for that session's **project directory** (the session `cwd`) — any later request in that
directory with the same agent kind, tool kind and title is allowed without asking, in any session;
`reject` picks `reject_once`. If the agent offered no option that carries the decision out, it is
answered `cancelled` (which never allows). Nothing waits forever: an **unattended** session
rejects anything nobody answers within `HERMARCHUS_UNATTENDED_TIMEOUT_S` (60 s), an attended one
within `HERMARCHUS_PERMISSION_TIMEOUT_S` (30 min) — `permission.resolved` with `by: "timeout"`.
Cancelling the turn, stopping the session or a host restart answers pending requests `cancelled`.

### The agent side — ACP

Hermarchus is an ACP **client** (protocol version 1, JSON-RPC 2.0, one message per line on the
agent's stdin/stdout). Per session it starts the kind's program in the session's working
directory, sends `initialize` (it advertises `fs.readTextFile` and `fs.writeTextFile`, not
`terminal`), refuses an agent that answers with another protocol version, opens one ACP session
(`session/new`, no MCP servers in v0), sends each turn as `session/prompt` and cancels with
`session/cancel`. It answers `session/request_permission` (above), `fs/read_text_file` and
`fs/write_text_file` — confined to the workspace — and records `session/update` notifications as
`agent.*` events. An agent that needs a sign-in (`-32000 auth_required` on `session/new`) fails the
session with a message naming its sign-in methods.

### Agent kinds

| Kind | Program | Status |
| --- | --- | --- |
| `echo` | built in: `python -m epicurus_hermarchus.echo_agent` | always available; what the tests and both smoke gates use |
| `claude-code` | `claude-agent-acp` — `npm install -g @agentclientprotocol/claude-agent-acp` | **not verified by running** |
| `codex` | `codex-acp` — `npm install -g @agentclientprotocol/codex-acp` | **not verified by running** |
| `grok-build` | `grok agent stdio` — `curl -fsSL https://x.ai/cli/install.sh \| bash` | **not verified by running** |

The three vendor entries are taken from each project's own documentation (sources below); nobody
has run them or signed in to them on a Hermarchus, and `/v1/info` reports `verified: false` for
them. **No vendor program is in the image.**

- Claude Code: <https://agentclientprotocol.com/overview/agents>,
  <https://github.com/agentclientprotocol/claude-agent-acp> (the entry sets
  `CLAUDE_AGENT_ACP_PROTOCOL=v1`, the adapter's documented switch to the stable protocol).
- Codex: <https://agentclientprotocol.com/overview/agents>,
  <https://github.com/agentclientprotocol/codex-acp>.
- Grok Build: <https://docs.x.ai/build/overview>, <https://github.com/xai-org/grok-build>; the
  `grok agent stdio` invocation is from the ACP registry page
  <https://zed.dev/acp/agent/grok-build>, not from xAI's own docs.

**`echo`**, the reference agent, is a real ACP agent written independently of the client (it
shares no code with it), so a session with it proves the adapter end to end. By the prompt's first
word: `permission [what]` asks for a permission and reports `permission: allowed (<option>)` /
`rejected` / `cancelled`; `write <path> <text>` and `read <path>` use the client file methods;
`tool [title]` emits a tool call and its updates; `plan` publishes a plan; `sleep <s>` thinks and
waits, cancellably (for steer, queue and cancel); `env` lists its environment variable *names*;
`exit [code]` exits; `fail` errors the turn; anything else is answered `echo: <prompt>`. If `echo`
works and a vendor kind does not, the fault is in that program or its sign-in.

## Configuration

Environment variables, prefixed `HERMARCHUS_` (plus the stack-wide `LOG_LEVEL`, `APP_ENV`,
`JSON_LOGS`). Full table: [reference](../reference/hermarchus.md#configuration).

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMARCHUS_TOKEN` / `_TOKEN_FILE` | — | **required**, ≥ 16 characters; the service refuses to start without it |
| `HERMARCHUS_PROFILE` | `local` | the bound profile; validated with `epicurus-core`'s tenant rule |
| `HERMARCHUS_WORKSPACE_DIR` | `/workspace` | the agents' workspace |
| `HERMARCHUS_STATE_DIR` | `/state` | the SQLite spool |
| `HERMARCHUS_AGENT_HOME` | `/home/hermarchus` | `HOME` for agents: installed programs and sign-ins |
| `HERMARCHUS_MAX_SESSIONS` | `10` | live sessions at once; beyond it 429 |
| `HERMARCHUS_STREAM_QUEUE_SIZE` | `1000` | events buffered per streaming connection |
| `HERMARCHUS_UNATTENDED` | `false` | default for a session's `unattended` |
| `HERMARCHUS_UNATTENDED_TIMEOUT_S` / `_PERMISSION_TIMEOUT_S` | `60` / `1800` | then a request is rejected |
| `HERMARCHUS_KINDS_FILE` | — | YAML of extra / replacing kinds |
| `HERMARCHUS_INSTALL_KINDS` | — | kinds to install into the home volume at start |
| `HERMARCHUS_CHILD_ENV_PASSTHROUGH` | — | extra variable names agents may inherit |

**Agent processes do not inherit the service's environment.** An agent starts with `PATH`,
`LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TERM` (when set), the names in
`HERMARCHUS_CHILD_ENV_PASSTHROUGH`, `HOME` = the home volume, `NPM_CONFIG_PREFIX` inside it, `PATH`
prefixed with `~/.local/bin` and `~/.npm-global/bin`, and the kind's own `env`. Every
`HERMARCHUS_*` name and any variable whose value is the bearer token is removed last, whatever put
it there. The service refuses to start when `HERMARCHUS_STATE_DIR` or `HERMARCHUS_AGENT_HOME`
overlaps `HERMARCHUS_WORKSPACE_DIR`.

## Security model — where the walls are

Agent programs run arbitrary code, and Hermarchus is **not an operating-system sandbox**: every
agent runs as the service's own user (uid 10001 in the image). **The container — or machine — is
the wall.** Inside it, as that user, any agent can:

- read and write the **whole workspace**, every session's directory, not only its own `cwd`;
- read and write the **home volume** — every installed agent program and **every sign-in** on
  it, and replace an installed program (the home volume's `bin` directories come first on the
  agent `PATH`, so a planted program runs in later sessions and suppresses its install);
- read and write the **state database** — every session's log, and forge rows in it (a
  remembered `always` rule, say). The permission relay is therefore consent for a cooperating
  agent program, not a wall against a hostile one.

What Hermarchus does keep from agents:

- **The bearer token.** It is not in an agent's environment (above), and at start the process
  makes itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`), so the same user cannot read
  `/proc/<service pid>/environ`, `mem` or `fd/` (`tests/test_entrypoint.py` proves it with an
  agent that tries). If you use `HERMARCHUS_TOKEN_FILE`, mount it where uid 10001 cannot read
  it after start, or agents can; the Compose overlay and the chart pass the token in the
  environment.
- **The stack.** On Compose the overlay puts Hermarchus on a network of its own (`hermarchus`),
  not the stack's `epicurus` network: agents reach the internet (their vendors, npm) but no
  module's MCP endpoint, not the core's platform API or model gateway (sign-in guards only
  requests that came through the web proxy), not the data plane — the `runtime-smoke` gate
  proves `core-app`, a module, Postgres and Qdrant do not answer from inside it. On Kubernetes
  the same holds **only with `networkPolicy.enabled`** on a CNI that enforces it: the pod may
  send to cluster DNS and public addresses only (`hermarchus.egressExtra` adds more), and no
  in-release policy admits it. With the chart's default (`networkPolicy` off), nothing stops an
  agent calling any Service in the cluster; this effect is not proven in CI (`k8s-smoke` runs
  with policies off).
- **Paths outside the workspace, through Hermarchus.** A session `cwd` and every path an agent
  passes to `fs/read_text_file` / `fs/write_text_file` must resolve, symlinks followed, inside the
  workspace. The check and the file operation are two steps, so an agent racing a symlink swap
  could slip between them — gaining nothing it could not already do as its own user.

## Data model

One SQLite file, `$HERMARCHUS_STATE_DIR/hermarchus.sqlite3` (WAL mode), every row carrying
`tenant_id` = the bound profile: `sessions`, `events` (`(session_id, seq)` primary key),
`inbox`, `permission_requests`, `permission_rules` (the remembered `always` answers, keyed by
profile, project directory, agent kind, tool kind and title). Its schema is versioned with
`PRAGMA user_version` (`schema.py`): ordered, append-only steps, each in one transaction with its
version bump; a database **newer** than the code is refused untouched. It is outside the Alembic
framework and the Postgres `migrations` gate by design — see
[migrations](../developer/migrations.md) for the rest of the stack.

Beside it, two volumes that are state by nature: the **workspace** (whatever the agents make) and
the **home** directory (installed agent programs, their configuration and sign-ins). On Compose
these are the named volumes `hermarchus-workspace`, `hermarchus-home`, `hermarchus-state`; on
Kubernetes three ReadWriteOnce claims. **Back them up** like any other state: a lost home volume
means reinstalling and signing in again.

On start, sessions a crash left `starting` / `idle` / `running` become `interrupted`, with
`input.dropped` / `permission.resolved` (`by: "host_restart"`) / `session.interrupted` events. On
shutdown (SIGTERM) open event streams get 5 s to close and are then cut, every agent is stopped
(stdin closed, then SIGTERM to its process group, then SIGKILL) and its session is marked
`interrupted` with `session.stopping` → `session.interrupted` — well inside the 30 s the Compose
overlay and the chart give the container (`test_entrypoint.py` holds a following stream open
across the SIGTERM).
v0 cannot resume an interrupted session; start a new one.

## Dependencies

`epicurus-core` (tenant validation, logging, `/health` + `/metrics`), FastAPI, SQLAlchemy +
aiosqlite, PyYAML. At runtime: nothing in the stack. Agent programs reach their vendors
themselves (outbound network); a subscription sign-in lives in the instance's home volume.

## Run & extend

**Compose** (opt-in overlay — the default `docker compose up` is unchanged):

```bash
export HERMARCHUS_TOKEN="$(openssl rand -hex 32)"
docker compose -f compose.yaml -f services/hermarchus/compose.yaml up -d hermarchus
# or: task hermarchus-up
curl -s -H "Authorization: Bearer $HERMARCHUS_TOKEN" http://127.0.0.1:8094/v1/info
```

**Kubernetes:** `--set hermarchus.enabled=true` — see
[Kubernetes](../infrastructure/kubernetes.md#hermarchus--the-agent-host).

**An echo session with curl:**

```bash
H=http://127.0.0.1:8094; A="Authorization: Bearer $HERMARCHUS_TOKEN"
SID=$(curl -s -H "$A" -H 'Content-Type: application/json' -X POST $H/v1/sessions \
  -d '{"agent":"echo","cwd":"demo"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
curl -s -H "$A" -H 'Content-Type: application/json' -X POST $H/v1/sessions/$SID/input \
  -d '{"text":"permission try something"}'
curl -s -H "$A" $H/v1/sessions/$SID/permissions        # take the "id"
curl -s -H "$A" -H 'Content-Type: application/json' -X POST \
  $H/v1/sessions/$SID/permissions/<id> -d '{"decision":"once"}'
curl -sN -H "$A" "$H/v1/sessions/$SID/events?after=0&follow=true"
```

**Installing an agent program and signing in.** Programs go into the home volume, never the
image. Either list the kinds in `HERMARCHUS_INSTALL_KINDS` (their `install` command runs at start,
in the background, as the service user with the agent environment; `/v1/info` shows each kind's
`install.state` — `succeeded`, `failed` with the output tail, or `skipped` when the command is
already there — and a failure is never fatal), or do it by hand inside the instance.

**What install-on-request opts you into.** Only the operator sets it — the environment (or the
chart's `hermarchus.installKinds`) picks the kinds and the shipped list or `HERMARCHUS_KINDS_FILE`
holds the commands; no API call can. Each command is a **shell string run through `/bin/sh -c`**,
with network access, on **every start** where the kind's program is not already on the agent
`PATH`. The shipped commands install the publisher's **current, unpinned release**
(`npm install -g …`), and Grok Build's pipes `https://x.ai/cli/install.sh` into `bash`. Turning it
on means trusting those publishers' latest release, fetched at start, with the instance's
sign-ins and workspace. By hand:

```bash
docker compose -f compose.yaml -f services/hermarchus/compose.yaml exec hermarchus bash
# Kubernetes: kubectl exec -it deploy/hermarchus -- bash
export NPM_CONFIG_PREFIX=$HOME/.npm-global PATH=$HOME/.npm-global/bin:$HOME/.local/bin:$PATH
npm install -g @agentclientprotocol/codex-acp
# then the vendor's own sign-in, once, as this user — e.g. run the vendor CLI and follow its
# login flow. Credentials land under $HOME, on the home volume, and stay there.
```

The sign-in step is the vendor's and is **not verified here**: no vendor program has been signed
in to on a Hermarchus. An agent whose sign-in is missing fails its session with the methods it
offers. API keys belong in the kind's `env` (via `HERMARCHUS_KINDS_FILE`) only if you accept they
live in that file; the plan in #1014 is that keys never enter a sandbox and agents are pointed at
the gateway with short-lived keys — not built in v0.

**Adding a kind.** Write a kinds file and point `HERMARCHUS_KINDS_FILE` at it:

```yaml
kinds:
  my-agent:
    name: My agent
    command: my-agent        # resolved on the agent PATH (home volume bins first)
    args: [--acp]
    env: {MY_AGENT_MODE: fast}
    install: npm install -g my-agent
  codex:
    enabled: false           # turn a shipped kind off
```

**Changing it safely.** The contract is versioned (`contract_version`, the `/v1` prefix): a
breaking change to a shape bumps both. A schema change is a new `STEPS[n]` in `schema.py` plus the
matching `store.py` table change — `tests/test_schema.py` holds the two together. The safety
requirements each have a test: workspace confinement (`test_sandbox.py`, `test_sessions.py`), the
child environment (`test_sandbox.py`, and `/proc/<pid>/environ` in `test_sessions.py`), the token
out of agents' reach through `/proc` (`test_entrypoint.py`), shutdown
and crash reconciliation (`test_lifecycle.py`), profile refusal and the token (`test_sessions.py`,
`test_settings.py`), never waiting forever (`test_permissions.py`), slow readers
(`test_streams.py`). Run them with `uv run pytest services/hermarchus`.

**What v0 leaves out:** a controller in the stack (nothing in Pamphilus or Epicurus drives it
yet), pairing and outward-dialled links, the network-path tunnel, the built-in agent as a kind,
terminals (`terminal/*` is not offered), MCP servers for agents (`session/new` sends none),
resuming an interrupted session (`session/load`), agent API keys via the gateway, and any UI.
