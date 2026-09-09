#!/usr/bin/env sh
# Runtime smoke gate (Docker Compose) — boot the data plane + core + modules and
# assert the integration last mile that lint / type-check / compose-validate never
# exercise.
#
# This script owns the **Compose-specific** half: build, boot, the real OpenBao
# bootstrap, the compose-only assertions (an external bind mount round-tripping,
# Docker control through docker-proxy-core, the one-shot init containers), restart
# and teardown. The runtime-neutral assertions — module discovery, status through
# core, MCP, attachments, the event spine, automations, secret persistence — live in
# infra/ci/smoke-assert.sh, which the Kubernetes gate (infra/ci/k8s-smoke.sh, #894)
# sources too, so the two gates assert the same things from one implementation.
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

# shellcheck disable=SC1007 # intentional: clears CDPATH so `cd` can't print an unexpected path
ROOT="$(CDPATH= cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-epicurus-ci}"
NET="$COMPOSE_PROJECT_NAME"
ENV_FILE="$(mktemp)"
SECRETS_FILE="$(mktemp)"
# External mounts round trip (#731): a real host directory bind-mounted into core-app (see
# infra/ci/compose.ci.yaml), not a system tmp path — Docker Desktop's default file-sharing
# roots don't always include the OS temp dir, but they always include the checkout itself
# (the build context already has to be reachable). Seeded with one pre-existing file so the
# assertions can check a list sees it, not only a round-tripped write.
#
# world-writable (0777): core-app runs as a non-root uid (10001, ADR-0069) inside the
# container, but this directory is created here by whatever user runs the CI job — on a
# real Linux runner those are different uids, and a bind mount does not remap ownership.
# Without this, `mkdir`'s default (umask-restricted) permissions leave the mount root
# unwritable by the container and the write assertion below 500s with a bare "Internal
# Server Error" — invisible on Docker Desktop for Windows, whose bind-mount translation
# does not enforce host-side ownership the same way, which is why this was missed locally.
SMOKE_MOUNT_DIR="$ROOT/.smoke-mount"
rm -rf "$SMOKE_MOUNT_DIR"
mkdir -p "$SMOKE_MOUNT_DIR"
chmod 0777 "$SMOKE_MOUNT_DIR"
printf 'seed\n' > "$SMOKE_MOUNT_DIR/seed.txt"
BOOT_LOG="$(mktemp)"
DC="docker compose -f compose.yaml -f infra/ci/compose.ci.yaml --env-file $ENV_FILE"
CURL_IMG="curlimages/curl:8.11.1"

DATA_PLANE="openbao postgres valkey nats qdrant minio minio-init"

# The runtime-neutral assertions, shared with the Kubernetes gate. Sourced early
# because it also owns `smoke_modules`, the canonical module set both gates boot.
# shellcheck source=infra/ci/smoke-assert.sh disable=SC1091 # linted on its own; CI lints one file at a time
. "$ROOT/infra/ci/smoke-assert.sh"

EXPECT_MODULES="$(smoke_modules)"
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

# ── the restart callbacks smoke-assert.sh calls (Compose's answer) ────────────
restart_openbao() { # bounce the vault and unseal it again
  unseal_key="$(grep -E '^OPENBAO_UNSEAL_KEY=' "$SECRETS_FILE" | tail -1 | cut -d= -f2-)"
  $DC restart openbao >/dev/null 2>&1
  i=0
  while [ "$i" -lt 30 ]; do
    $DC exec -T openbao bao status >/dev/null 2>&1 && break
    $DC exec -T openbao bao operator unseal "$unseal_key" >/dev/null 2>&1 || true
    i=$((i + 1))
    sleep 2
  done
  wait_state openbao
}

restart_core_app() {
  $DC restart core-app >/dev/null 2>&1
  wait_state core-app
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
    rm -rf "$SMOKE_MOUNT_DIR"  # still bind-mounted into core-app while KEEP_UP holds it up
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
SMOKE_MOUNT_DIR=$SMOKE_MOUNT_DIR
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
  # shellcheck disable=SC2086 # $APP is a deliberate space-separated service-name list (no
  # arrays in POSIX sh — positional-style word splitting is the portable stand-in, #691)
  $DC build $APP
fi

log "Starting the data plane"
# shellcheck disable=SC2086 # $DATA_PLANE: same deliberate word list as $APP above
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

# The app token must be *periodic* (#728). A plain service token also works here — and then
# 403s on every secret read 32 days later, long after any test has finished. That expiry is
# untestable by construction, so assert the property that prevents it: a non-zero period.
app_token="$(grep -E '^OPENBAO_TOKEN=' "$SECRETS_FILE" | tail -1 | cut -d= -f2-)"
token_period="$($DC exec -T -e BAO_TOKEN="$app_token" openbao \
    bao token lookup -format=json 2>/dev/null | tr -d ' \t\r\n' \
    | grep -o '"period":[0-9]*' | cut -d: -f2)"
case "$token_period" in
  ''|*[!0-9]*|0) die "app token is not periodic (period='${token_period:-unset}') — it expires, see #728" ;;
esac
ok "app token is periodic (${token_period}s), so renewal can keep it alive indefinitely"

log "Starting the auto-unseal sidecar, core, and modules"
$DC up -d openbao-unseal
# shellcheck disable=SC2086 # $APP: same deliberate word list as above
$DC up -d $APP
for s in core-app $EXPECT_MODULES; do
  wait_state "$s"
  ok "$s healthy"
done
us="$(docker inspect -f '{{.State.Status}}' "$($DC ps -q openbao-unseal)" 2>/dev/null || true)"
[ "$us" = "running" ] || die "openbao-unseal is '$us' (expected running — OPENBAO_UNSEAL_KEY wiring?)"
ok "openbao-unseal running (not crash-looping)"

# ── Compose-only assertions ───────────────────────────────────────────────────
# What only a Compose deployment can be asked: a host bind mount, the Docker
# control path, and the one-shot init containers. Everything runtime-neutral is
# asserted by smoke_assert (infra/ci/smoke-assert.sh) at the end.
log "Asserting the Compose-specific last mile"

http -f "http://core-app:8080/health" >/dev/null || die "core-app /health unreachable or non-200"
ok "core-app /health"

# External mounts (#731): a declared RW mount round-trips list/write/read through the platform
# API against the real bind-mounted directory (infra/ci/compose.ci.yaml) — the one thing a
# unit test (LocalFileStore directly, no container) and compose-validate (YAML only, no `up`)
# can't prove: the container actually sees the host bytes, and a write through the API
# actually lands on disk, not just in an in-memory fake.
mlist="$(http "http://core-app:8080/platform/v1/files/list?path=mount:smoketest")"
printf '%s' "$mlist" | grep -q '"name":"seed.txt"' \
  || die "declared mount did not list its pre-existing seed file: $mlist"
ok "external mount lists pre-existing host content (#731)"

mwrite="$(http -X PUT "http://core-app:8080/platform/v1/files/write?path=mount:smoketest/hello.txt" \
  -H 'Content-Type: application/json' -d '{"content":"hello from smoke"}' || true)"
printf '%s' "$mwrite" | grep -q '"path":"mount:smoketest/hello.txt"' \
  || die "write into a declared RW mount failed: $mwrite"
[ -f "$SMOKE_MOUNT_DIR/hello.txt" ] || die "mount write did not land on the real host directory"
[ "$(cat "$SMOKE_MOUNT_DIR/hello.txt")" = "hello from smoke" ] \
  || die "mount write landed with the wrong content"
ok "write through the platform API lands on the real host directory"

mread="$(http "http://core-app:8080/platform/v1/files/read?path=mount:smoketest/hello.txt")"
printf '%s' "$mread" | grep -q '"content":"hello from smoke"' \
  || die "read-after-write through a mount did not round-trip: $mread"
ok "read-after-write round-trips through an external mount (#731)"

# Least-privilege Docker control by default (#708, ADR-0109, and the Docker arm of the
# container-runtime seam #891): docker-status must report reachable with no operator setup
# (the proxy, not the opt-in raw socket), and a real KV-cache apply must round-trip through
# it — set, then clear, so the shared ollama-runtime volume is left as this run found it.
# This is the acceptance check the issue itself names. The Kubernetes arm of the same seam
# is asserted by infra/ci/k8s-smoke.sh.
ds="$(http "http://core-app:8080/platform/v1/modules/docker-status" || true)"
printf '%s' "$ds" | grep -q '"available":true' \
  || die "docker-status reports unreachable by default (docker-proxy-core not wired?): $ds"
ok "core reaches Docker by default through docker-proxy-core (#708)"

kv="$(http -X PUT "http://core-app:8080/platform/v1/llm/prefs/kv-cache-type" \
  -H 'Content-Type: application/json' -d '{"value":"q8_0"}' || true)"
printf '%s' "$kv" | grep -q '"applied":true' \
  || die "KV-cache change did not apply through docker-proxy-core (restart round-trip broken?): $kv"
ok "KV-cache change applied immediately — restart round-tripped through docker-proxy-core (#708)"
http -X PUT "http://core-app:8080/platform/v1/llm/prefs/kv-cache-type" \
  -H 'Content-Type: application/json' -d '{"value":null}' >/dev/null 2>&1 || true
# Both calls above restart ollama through the proxy. Later assertions do reach the model — the
# automations run (#666, in smoke_assert) goes agent -> LLM -> ollama — so block until it is
# back rather than racing a cold container from here on.
wait_state ollama

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

# ── the runtime-neutral last mile, shared with the Kubernetes gate ─────────────
smoke_assert

log "ALL SMOKE CHECKS PASSED"
