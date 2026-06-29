#!/usr/bin/env sh
# Runtime smoke gate — boot the data plane + core + modules and assert the
# integration last mile that lint / type-check / compose-validate never exercise:
#
#   * the OpenBao bootstrap runs clean and a secret survives a vault restart   (#93)
#   * core-app comes up healthy and discovers every module via module_urls     (#68)
#   * each module's status_url is reachable THROUGH core                       (#92)
#   * one MCP tool round-trips through core
#   * an attachable module's chat-attachment picker round-trips through core    (#136)
#
# Every recent module PR passed CI green and then broke on first real boot for one
# of these reasons (see #98). This gate boots the actual stack and fails if any of
# them regress.
#
# Isolated by design — its own compose project + network and no published host
# ports (see infra/ci/compose.ci.yaml) — so it runs on a fresh CI runner or right
# next to a developer's dev stack. Services are reached over the internal network
# through a throwaway curl container. Tears down on exit unless KEEP_UP=1.
#
#   sh infra/ci/smoke.sh                     # build, boot, assert, tear down
#   KEEP_UP=1 sh infra/ci/smoke.sh           # leave it running afterwards to poke at
#   SMOKE_SKIP_BUILD=1 sh infra/ci/smoke.sh  # images already built (CI builds first)
set -eu

ROOT="$(CDPATH= cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-epicurus-ci}"
NET="$COMPOSE_PROJECT_NAME"
ENV_FILE="$(mktemp)"
SECRETS_FILE="$(mktemp)"
BOOT_LOG="$(mktemp)"
DC="docker compose -f compose.yaml -f infra/ci/compose.ci.yaml --env-file $ENV_FILE"
CURL_IMG="curlimages/curl:8.11.1"

DATA_PLANE="openbao postgres valkey nats qdrant minio minio-init"
# Modules are derived from the root compose `include:` list (services/<name>/...),
# minus the core app and the web shell — so a module is gated the moment it is
# wired into compose.yaml, with no edit to this script.
EXPECT_MODULES="$(
  grep -oE 'services/[a-z0-9-]+/compose\.yaml' compose.yaml |
    sed -E 's#services/([a-z0-9-]+)/.*#\1#' |
    grep -vxE 'core-app|web' | sort -u | tr '\n' ' '
)"
APP="core-app $EXPECT_MODULES"

# ── output helpers ────────────────────────────────────────────────────────────
log() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mSMOKE FAILED: %s\033[0m\n' "$*" >&2; exit 1; }

# HTTP against the internal network — no host ports needed.
http() { docker run --rm --network "$NET" "$CURL_IMG" -s --max-time 25 "$@"; }

wait_state() { # <service> — block until healthy (or running, if it has no healthcheck)
  svc="$1"
  cid="$($DC ps -q "$svc" 2>/dev/null || true)"
  [ -n "$cid" ] || die "no container for service '$svc'"
  i=0
  while [ "$i" -lt 60 ]; do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    case "$st" in
      healthy | running) return 0 ;;
      exited | dead) die "service '$svc' is '$st'" ;;
    esac
    i=$((i + 1))
    sleep 2
  done
  die "service '$svc' never became healthy (last state: ${st:-unknown})"
}

dump_diagnostics() {
  log "Diagnostics (smoke failed)"
  $DC ps || true
  for s in openbao openbao-unseal core-app $EXPECT_MODULES searxng; do
    printf '\n--- logs: %s ---\n' "$s"
    $DC logs --tail 40 "$s" 2>&1 || true
  done
}

cleanup() {
  rc=$?
  [ "$rc" -ne 0 ] && dump_diagnostics
  if [ "${KEEP_UP:-0}" = "1" ]; then
    log "KEEP_UP=1 — leaving '$COMPOSE_PROJECT_NAME' up (tear down: $DC down -v)"
  else
    log "Tearing down"
    $DC down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -f "$ENV_FILE" "$SECRETS_FILE" "$BOOT_LOG"
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── minimal env (everything else comes from the compose `:-` defaults) ─────────
cat > "$ENV_FILE" <<EOF
APP_ENV=ci
LOG_LEVEL=warning
DEFAULT_TENANT_ID=local
EOF

log "Smoke project '$COMPOSE_PROJECT_NAME' (network $NET, no host ports)"
docker pull -q "$CURL_IMG" >/dev/null

# Pre-flight: two fragments publishing the same host port is the #68 collision class.
# The smoke itself clears ports (for isolation), so check the real compose instead.
log "Pre-flight: checking for duplicate published host ports"
dupes="$(docker compose -f compose.yaml config 2>/dev/null |
  grep -oE 'published: "?[0-9]+' | grep -oE '[0-9]+' | sort | uniq -d | tr '\n' ' ')"
[ -z "$dupes" ] || die "two services publish the same host port(s): $dupes — pick a unique one"
ok "no duplicate published host ports"

if [ "${SMOKE_SKIP_BUILD:-0}" != "1" ]; then
  log "Building service images from this checkout"
  $DC build $APP
fi

log "Starting the data plane"
$DC up -d $DATA_PLANE

log "Running the real OpenBao bootstrap (infra/compose/scripts/openbao-bootstrap.sh)"
# Capture so we can redact the ephemeral key/token the script prints (the vault is
# torn down at the end, but key-shaped strings don't belong in CI logs).
redact() { sed -E 's/^( *OPENBAO_(UNSEAL_KEY|TOKEN)=).*/\1<redacted>/'; }
if COMPOSE_FILE="compose.yaml" SECRETS_FILE="$SECRETS_FILE" \
    sh infra/compose/scripts/openbao-bootstrap.sh >"$BOOT_LOG" 2>&1; then
  redact <"$BOOT_LOG"
else
  rc=$?; redact <"$BOOT_LOG"; die "OpenBao bootstrap failed (exit $rc)"
fi
grep -E '^OPENBAO_(UNSEAL_KEY|TOKEN)=' "$SECRETS_FILE" >> "$ENV_FILE"
wait_state openbao
ok "OpenBao bootstrapped, unsealed, healthy"

log "Starting the auto-unseal sidecar, core, and modules"
$DC up -d openbao-unseal
$DC up -d $APP
for s in core-app $EXPECT_MODULES; do
  wait_state "$s"
  ok "$s healthy"
done
us="$(docker inspect -f '{{.State.Status}}' "$($DC ps -q openbao-unseal)" 2>/dev/null || true)"
[ "$us" = "running" ] || die "openbao-unseal is '$us' (expected running — OPENBAO_UNSEAL_KEY wiring?)"
ok "openbao-unseal running (not crash-looping)"

# ── assertions ────────────────────────────────────────────────────────────────
log "Asserting the integration last mile"

http -f "http://core-app:8080/health" >/dev/null || die "core-app /health unreachable or non-200"
ok "core-app /health"

mods="$(http "http://core-app:8080/platform/v1/modules")"
for m in $EXPECT_MODULES; do
  printf '%s' "$mods" | grep -q "\"$m\"" || die "module '$m' missing from core's registry (module_urls?)"
done
ok "core discovered every module: $EXPECT_MODULES"

for m in $EXPECT_MODULES; do
  s="$(http "http://core-app:8080/platform/v1/modules/$m/status" || true)"
  case "$s" in
    *"no reachable"*) die "module '$m' is registered but unreachable via core: $s" ;;
    *"has no status_url"*) : ;; # fine — the module declares no status endpoint
    *"{"*) : ;;                 # a JSON status body — reachable
    *) die "module '$m' status returned an unexpected response: $s" ;;
  esac
done
ok "every module reachable through core (live status where declared)"

rdy="$(http "http://core-app:8080/platform/v1/readiness" || true)"
printf '%s' "$rdy" | grep -q '"components"' || die "readiness endpoint returned no snapshot: $rdy"
printf '%s' "$rdy" | grep -q '"power"' || die "readiness snapshot missing power state: $rdy"
ok "readiness endpoint serves a warming snapshot (ADR-0027)"

# Model catalog (#269): the endpoint must serve a non-empty snapshot the instant core is
# up — the built-in seed guarantees entries even before (or without) the upstream fetch, so
# this asserts the wiring without depending on CI having outbound internet.
cat="$(http "http://core-app:8080/platform/v1/llm/catalog" || true)"
printf '%s' "$cat" | grep -q '"entries"' || die "model catalog endpoint returned no snapshot: $cat"
printf '%s' "$cat" | grep -q '"id"' || die "model catalog served no entries (seed missing?): $cat"
ok "model catalog endpoint serves entries (seed or live, #269)"

# qdrant upgrade-recovery guard (#229): the one-shot must complete cleanly, and the
# new /proc-based healthcheck must report healthy (a crash-looping qdrant binds no port
# and would be unhealthy). compose-validate can't see either — only a live boot can.
qi_cid="$($DC ps -aq qdrant-init 2>/dev/null || true)"
[ -n "$qi_cid" ] || die "qdrant-init container not found — the volume guard is not wired"
qi_rc="$(docker inspect -f '{{.State.ExitCode}}' "$qi_cid" 2>/dev/null || echo 1)"
[ "$qi_rc" = "0" ] || die "qdrant-init exited $qi_rc (volume guard failed)"
wait_state qdrant
ok "qdrant-init completed and qdrant is healthy via the /proc HTTP-listener check (#229)"

# ollama-runtime ownership guard (#392): the one-shot must complete cleanly so the
# root-owned fresh volume is chowned to uid 10001 — otherwise the core (uid 10001) can't
# write /etc/epicurus/ollama.env to apply a KV-cache change. ollama depends on it, so a
# stack boot runs it; compose-validate can't see the chown, only a live boot can.
oi_cid="$($DC ps -aq ollama-init 2>/dev/null || true)"
[ -n "$oi_cid" ] || die "ollama-init container not found — the volume-ownership chown is not wired"
oi_rc="$(docker inspect -f '{{.State.ExitCode}}' "$oi_cid" 2>/dev/null || echo 1)"
[ "$oi_rc" = "0" ] || die "ollama-init exited $oi_rc (volume-ownership chown failed)"
ok "ollama-init completed and chowned the ollama-runtime volume to uid 10001 (#392)"

ws="$(http -X POST "http://core-app:8080/platform/v1/modules/websearch/tools/web_search" \
  -H 'Content-Type: application/json' -d '{"arguments":{"query":"epicurus"}}' || true)"
printf '%s' "$ws" | grep -q '"result"' || die "web_search tool did not round-trip: $ws"
ok "web_search MCP tool round-tripped through core"

# Editable calendar (#208): a write tool round-trips through the core onto the local
# store (no model needed), and the page archetype carries the create/edit/delete actions
# the shell renders. Exercises the full read-write path that unit tests can't (the proxy).
cc="$(http -X POST "http://core-app:8080/platform/v1/modules/calendar/tools/calendar_create_event" \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"title":"Smoke check","start":"2030-01-01T10:00:00+00:00","end":"2030-01-01T11:00:00+00:00"}}' || true)"
printf '%s' "$cc" | grep -q 'Smoke check' || die "calendar_create_event did not round-trip: $cc"
cpage="$(http "http://core-app:8080/platform/v1/modules/calendar/pages/calendar?start=2030-01-01T00:00:00%2B00:00&end=2030-01-02T00:00:00%2B00:00" || true)"
printf '%s' "$cpage" | grep -q 'calendar_create_event' || die "calendar page missing create action: $cpage"
printf '%s' "$cpage" | grep -q 'calendar_delete_event' || die "calendar page event missing delete action: $cpage"
ok "editable calendar: create tool + page actions round-tripped through core (#208)"

# Every attachable module's chat-attachment picker (ADR-0019, #136) must round-trip
# through the core — the only path a note/doc/event reaches the agent. The picker is
# the attach surface the core exposes as a route; resolve runs in-process per turn.
# Modules that declare no attach surface answer "not attachable" (fine); a healthy
# attachable module returns a JSON array. At least one must, or the proxy is broken.
attach_seen=0
for m in $EXPECT_MODULES; do
  a="$(http "http://core-app:8080/platform/v1/modules/$m/attachments" || true)"
  case "$a" in
    '['*) attach_seen=$((attach_seen + 1)) ;;   # a JSON array — an attachable picker
    *'is not attachable'*) : ;;                 # module declares no attach surface — fine
    *) die "module '$m' attachment picker returned an unexpected response: $a" ;;
  esac
done
[ "$attach_seen" -gt 0 ] || die "no attachable module served a picker through core (attach proxy broken?)"
ok "chat-attachment picker round-tripped through core ($attach_seen attachable module(s))"

# OpenBao secret persistence across a vault (and core) restart.
. "$SECRETS_FILE"
http -X PUT "http://core-app:8080/platform/v1/llm/providers/claude/key" \
  -H 'Content-Type: application/json' -d '{"api_key":"ci-smoke-DO-NOT-USE"}' \
  | grep -q '"status":"ok"' || die "setting a provider key via core failed (OpenBao write/auth)"
$DC restart openbao >/dev/null 2>&1
i=0
while [ "$i" -lt 30 ]; do
  $DC exec -T openbao bao status >/dev/null 2>&1 && break
  $DC exec -T openbao bao operator unseal "$OPENBAO_UNSEAL_KEY" >/dev/null 2>&1 || true
  i=$((i + 1))
  sleep 2
done
wait_state openbao
$DC restart core-app >/dev/null 2>&1
wait_state core-app
prov="$(http "http://core-app:8080/platform/v1/llm/providers")"
printf '%s' "$prov" | grep -oE '"alias":"claude"[^}]*' | grep -q '"configured":true' \
  || die "provider key did not survive an OpenBao + core-app restart"
ok "provider key survived a vault + core restart (persistent secrets)"

log "ALL SMOKE CHECKS PASSED"
