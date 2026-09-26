#!/usr/bin/env sh
# The integration last mile, asserted identically on every runtime we ship.
#
# This file holds the *assertions* only — the half that is true of an epicurus
# deployment whatever it is deployed on. The runtime-specific half (build, boot,
# wait, restart, tear down, diagnose) lives in the gate that sources this:
#
#   * infra/ci/smoke.sh      — Docker Compose  (the `runtime-smoke` CI job)
#   * infra/ci/k8s-smoke.sh  — Kubernetes/kind (the `k8s-smoke` CI job, #894)
#
# One implementation, two gates, so the two can never drift: an assertion added
# here is added to both, and a module wired into the stack is gated on both the
# moment it lands. What is asserted:
#
#   * core-app comes up healthy and discovers every module via module_urls     (#68)
#   * each module's status_url is reachable THROUGH core                       (#92)
#   * the web shell proxies /platform/ to the core through the runtime's DNS    (#891)
#   * the tenant file space round-trips write/read/list on its real storage     (#919)
#   * a KV-cache change restarts the LLM runtime through the container seam     (#307)
#   * one MCP tool round-trips through core
#   * an attachable module's chat-attachment picker round-trips through core    (#136)
#   * a module event reaches the durable log and the raw feed                   (#662)
#   * an event-triggered automation runs, and its safety gates hold             (#666)
#   * a secret set through core survives a vault restart                        (#93)
#
# and, in a second phase each gate runs last (`smoke_assert_sign_in`, #969):
#
#   * with AUTH_MODE=oidc and an unreachable issuer the core still starts (discovery is lazy)
#   * the web door answers the sign-in session as signed out, 401s the platform API, and
#     sends a sign-in attempt back with auth_error=provider_unreachable
#   * the core's own port still answers directly, and module <-> core traffic still works
#
# SOURCED, never executed — it defines three functions and runs nothing at import.
# The sourcing gate must provide, before calling `smoke_assert`:
#
#   http <curl-args…>   run curl against the stack's internal network, print the body
#                       (a throwaway curl container on compose; a curl pod in the
#                       namespace on Kubernetes) — must propagate curl's exit code
#   log / ok / die      the output helpers (die must exit non-zero)
#   restart_openbao     restart the vault, block until it is unsealed and healthy
#   restart_core_app    restart core-app, block until it is healthy again
#   settle_llm_runtime  block until the Ollama workload is back after a restart
#   enable_sign_in      restart core-app with AUTH_MODE=oidc, an unreachable issuer, a
#                       dummy client id and OIDC_ALLOW_ALL_USERS=true; block until healthy
#   EXPECT_MODULES      space-separated module names this deployment should have
#                       (`smoke_modules` below derives the canonical list)
#
# Every hook named above is checked by tests/test_smoke_gates.py against both gates.

# The module set both runtimes are gated on, derived from the root compose
# `include:` list (services/<name>/...) minus the core app and the web shell — so a
# module is gated the moment it is wired into compose.yaml, with no edit to either
# script. The chart is held to the same set by tests/test_chart_services.py, which
# fails if the chart's workloads and this list ever disagree; using it for the
# Kubernetes gate too makes that a live check rather than a static one.
smoke_modules() {
  grep -oE 'services/[a-z0-9-]+/compose\.yaml' compose.yaml |
    sed -E 's#services/([a-z0-9-]+)/.*#\1#' |
    grep -vxE 'core-app|web' | sort -u | tr '\n' ' '
}

smoke_assert() {
  : "${EXPECT_MODULES:?smoke_assert: the caller must set EXPECT_MODULES}"

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

  # The web shell, on every runtime. nginx derives its upstream resolver from the
  # container's own /etc/resolv.conf at start (#891) — Docker's embedded DNS under
  # Compose, cluster DNS in a pod — and when that is wrong BOTH probes stay green
  # while every proxied request dies at name resolution. Only a live boot can ask.
  # It lived in the Kubernetes gate alone until #919; the Compose gate never even
  # started `web`, so the Docker half of the same code path was ungated.
  http -f "http://web:8080/healthz" >/dev/null || die "web /healthz unreachable or non-200"
  winfo="$(http "http://web:8080/platform/v1/info" || true)"
  printf '%s' "$winfo" | grep -q '"core_app_version"' \
    || die "web did not proxy /platform/ to the core (nginx resolver wrong for this runtime?): $winfo"
  ok "the web shell proxies /platform/ to the core through the runtime's own DNS (#891)"

  # The tenant file space, through the platform API onto whatever really backs it: a
  # named volume under Compose, a ReadWriteOnce PVC in a cluster. Unit tests drive
  # LocalFileStore directly and so say nothing about the mount the deployment hands the
  # core — and the failure is uid-shaped (the core runs as 10001, ADR-0069), which only
  # a real filesystem can show. The Compose gate additionally round-trips a *declared
  # external mount* (#731); this is the tenant tree, which every runtime has.
  FILE_PATH="smoke-files-$$.txt"
  fw="$(http -X PUT "http://core-app:8080/platform/v1/files/write?path=$FILE_PATH" \
    -H 'Content-Type: application/json' -d '{"content":"file space round trip"}' || true)"
  printf '%s' "$fw" | grep -q "\"path\":\"$FILE_PATH\"" \
    || die "writing into the tenant file space failed (volume/PVC not writable by uid 10001?): $fw"
  fr="$(http "http://core-app:8080/platform/v1/files/read?path=$FILE_PATH" || true)"
  printf '%s' "$fr" | grep -q 'file space round trip' \
    || die "read-after-write through the tenant file space did not round-trip: $fr"
  fl="$(http "http://core-app:8080/platform/v1/files/list" || true)"
  printf '%s' "$fl" | grep -q "\"name\":\"$FILE_PATH\"" \
    || die "the file just written is not listed at the tenant root: $fl"
  ok "the tenant file space round-trips write/read/list on its real storage (#919)"

  # The operator's KV-cache choice is applied by *restarting* the LLM runtime through
  # the container-runtime seam (#307, #891, ADR-0134): Docker via docker-proxy-core
  # under Compose, a rollout-restart through the namespace-scoped Role in a cluster.
  # `applied:true` is the whole assertion — it means a workload was found and actually
  # restarted, which is the one thing the seam's mock-transport unit tests cannot say.
  # It does NOT claim the new value reached the server: in a cluster the env file sits
  # on an emptyDir only the core can see (a documented limitation, see
  # docs/infrastructure/kubernetes.md#known-limitations). The restart is the assertion.
  # Set, then clear, so the shared runtime state is left as this run found it.
  kv="$(http -X PUT "http://core-app:8080/platform/v1/llm/prefs/kv-cache-type" \
    -H 'Content-Type: application/json' -d '{"value":"q8_0"}' || true)"
  printf '%s' "$kv" | grep -q '"applied":true' \
    || die "the KV-cache change was staged but not applied — the restart path is broken: $kv"
  ok "a KV-cache change restarted the LLM runtime through the container-runtime seam (#307)"
  http -X PUT "http://core-app:8080/platform/v1/llm/prefs/kv-cache-type" \
    -H 'Content-Type: application/json' -d '{"value":null}' >/dev/null 2>&1 || true
  # Both calls above restart the LLM workload. A later assertion does reach it — the
  # automations run (#666) goes agent -> LLM -> Ollama — so block until it is back
  # rather than racing a cold container/pod from here on.
  settle_llm_runtime

  # Messaging foundation (ADR-0058): the module's status must report the active bridge, proving
  # the provider seam is wired at runtime. The inbound->turn->outbound path itself needs a model,
  # so it is proven in pytest (real NATS, faked turn), not here — the smoke stack runs no LLM.
  ms="$(http "http://core-app:8080/platform/v1/modules/messaging/status" || true)"
  printf '%s' "$ms" | grep -q 'loopback' || die "messaging status did not report the loopback bridge: $ms"
  ok "messaging module reports the loopback bridge (ADR-0058)"

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

  # Module event spine (#662, ADR-0103) — the acceptance gate for the whole chain:
  # emit -> NATS -> the core's cross-tenant intake -> the durable log -> the raw feed.
  # Unit tests fake the bus, so this is the only place the `*.events.>` wildcard is proven
  # against a real broker — and that failure mode is silent (a wildcard that matches nothing
  # produces no error, no log, and an intake that looks healthy and records nothing).
  # echo_ping is the spine's reference emitter; a fixed dedup_key also proves the log's
  # idempotency, which is the one property a single emit can never demonstrate.
  PING_KEY="smoke-$$"
  ping_body="{\"arguments\":{\"note\":\"smoke\",\"dedup_key\":\"$PING_KEY\"}}"
  ep="$(http -X POST "http://core-app:8080/platform/v1/modules/echo/tools/echo_ping" \
    -H 'Content-Type: application/json' -d "$ping_body" || true)"
  printf '%s' "$ep" | grep -q "$PING_KEY" || die "echo_ping did not round-trip through core: $ep"

  # Delivery is asynchronous (durable pull consumer, at-least-once since #841) — poll, never assume.
  i=0
  ev=""
  while [ "$i" -lt 20 ]; do
    ev="$(http "http://core-app:8080/platform/v1/events?module=echo" || true)"
    printf '%s' "$ev" | grep -q "\"dedup_key\":\"$PING_KEY\"" && break
    i=$((i + 1))
    sleep 1
  done
  printf '%s' "$ev" | grep -q "\"dedup_key\":\"$PING_KEY\"" \
    || die "echo.pinged never reached the durable event log (emit -> intake broken?): $ev"
  printf '%s' "$ev" | grep -q '"type":"echo.pinged"' || die "logged event has the wrong type: $ev"
  printf '%s' "$ev" | grep -q '"module":"echo"' || die "logged event has the wrong module: $ev"
  printf '%s' "$ev" | grep -q '"entity_ref"' || die "logged event dropped its entity_ref: $ev"
  ok "echo.pinged reached the core's durable event log (emit -> NATS -> intake -> log, #662)"

  # The same change announced twice is one event: the log dedups on (tenant, module, key).
  http -X POST "http://core-app:8080/platform/v1/modules/echo/tools/echo_ping" \
    -H 'Content-Type: application/json' -d "$ping_body" >/dev/null 2>&1 || true
  sleep 3  # let the duplicate land and be rejected — asserting too early would pass either way
  logged="$(http "http://core-app:8080/platform/v1/events?module=echo" \
    | grep -o "\"dedup_key\":\"$PING_KEY\"" | wc -l | tr -d ' ')"
  [ "$logged" = "1" ] || die "duplicate emission was stored $logged times, expected 1 (dedup broken?)"
  ok "a duplicate emission was stored once (dedup on tenant+module+dedup_key, #662)"

  # The raw feed replays it: history first, so a short read is enough to see it.
  # -N disables curl's buffering; the shorter --max-time wins and ends the (endless) stream.
  feed="$(http --max-time 6 -N "http://core-app:8080/platform/v1/events/stream?module=echo" || true)"
  printf '%s' "$feed" | grep -q '^event: module_event' || die "events feed served no SSE frame: $feed"
  printf '%s' "$feed" | grep -q "$PING_KEY" || die "events feed did not replay the logged event: $feed"
  ok "the raw events feed replayed it as SSE (log -> feed, #662)"

  # Automations (#666, ADR-0105) — the acceptance gate: an echo.pinged Notify automation
  # runs end to end on a fresh stack, and its safety gates hold. The matcher, the durable
  # queue, and the ledger are all real here; only the model is absent (the CI stack has
  # none), which is why a run's *outcome* is not asserted — that it ran, was recorded, and
  # was correctly refused when it should be, is what unit tests cannot prove.
  vocab="$(http "http://core-app:8080/platform/v1/automations/vocabulary" || true)"
  printf '%s' "$vocab" | grep -q '"silent_act"' || die "automations vocabulary missing: $vocab"
  ok "the automations vocabulary is served (the UI never hardcodes it, #666)"

  # A module's preset automation reaches the Templates tab — and creates nothing.
  tpl="$(http "http://core-app:8080/platform/v1/automations/templates" || true)"
  printf '%s' "$tpl" | grep -q '"key":"on-ping"' || die "echo's automation template is not offered: $tpl"
  live="$(http "http://core-app:8080/platform/v1/automations" || true)"
  printf '%s' "$live" | grep -q '"key":"on-ping"' \
    && die "a template was auto-instantiated — installing a module must never start an automation"
  ok "a module template is offered but never auto-instantiated (#666)"

  # Starter templates for every module (#705) reach the Templates tab through the *real*
  # module registry fan-out — proof this actually works on a live stack, not just the fake
  # `templates()` lookup core-app's own unit tests inject. TemplateView serializes `module`
  # before `key`, so one pattern per pair is enough.
  for pair in "mail:on-mail-received" "calendar:tomorrow-at-a-glance" "tasks:due-today-digest" \
    "notes:weekly-notes-review" "knowledge:on-large-vault-sync"; do
    mod="${pair%%:*}"
    key="${pair#*:}"
    printf '%s' "$tpl" | grep -q "\"module\":\"$mod\"[^}]*\"key\":\"$key\"" \
      || die "$mod's starter template '$key' is not offered: $tpl"
  done
  ok "every module's starter automation templates reach the Templates tab (#705)"

  # Create a Notify automation on echo.pinged, then ping and watch a run appear.
  auto="$(http -X POST "http://core-app:8080/platform/v1/automations" \
    -H 'Content-Type: application/json' \
    -d '{"name":"smoke notify","prompt":"An echo ping arrived.","autonomy":"notify",
         "event_trigger":{"module":"echo","event_type":"echo.pinged"},"sinks":["chat"]}' || true)"
  AUTO_ID="$(printf '%s' "$auto" | sed -n 's/.*"id":"\([a-f0-9]*\)".*/\1/p')"
  [ -n "$AUTO_ID" ] || die "could not create an automation: $auto"
  printf '%s' "$auto" | grep -q '"allowed_tool_classes":\["read"\]' \
    || die "a notify automation was not read-only: $auto"
  ok "a Notify automation was created and is read-only at the tool surface (#666)"

  # The kill switch halts everything — asserted BEFORE the happy path, so a run that
  # sneaks through is caught rather than masked by an earlier success.
  http -X PUT "http://core-app:8080/platform/v1/automations/kill-switch" \
    -H 'Content-Type: application/json' -d '{"halted":true}' >/dev/null 2>&1 || true
  halted_run="$(http -X POST "http://core-app:8080/platform/v1/automations/$AUTO_ID/run" || true)"
  printf '%s' "$halted_run" | grep -q 'kill switch' \
    || die "the kill switch did not halt a manual run: $halted_run"
  http -X PUT "http://core-app:8080/platform/v1/automations/kill-switch" \
    -H 'Content-Type: application/json' -d '{"halted":false}' >/dev/null 2>&1 || true
  ok "the kill switch halts a run, and resuming restores it (#666)"

  # Now the chain: ping -> intake -> matcher -> queue -> drain -> run -> ledger. The
  # scheduler's drain is on a poll interval, so trigger the run directly and assert the
  # matcher queued it; together those cover the same path without waiting a minute.
  http -X POST "http://core-app:8080/platform/v1/modules/echo/tools/echo_ping" \
    -H 'Content-Type: application/json' -d '{"arguments":{"note":"automation smoke"}}' \
    >/dev/null 2>&1 || true
  sleep 3  # the matcher runs inline with intake, but delivery to intake is asynchronous
  run="$(http -X POST "http://core-app:8080/platform/v1/automations/$AUTO_ID/run" || true)"
  printf '%s' "$run" | grep -q '"automation_id"' || die "an automation run produced no ledger entry: $run"
  printf '%s' "$run" | grep -q '"filter_verdict":"manual"' || die "wrong verdict recorded: $run"
  ledger="$(http "http://core-app:8080/platform/v1/automations/runs?automation_id=$AUTO_ID" || true)"
  printf '%s' "$ledger" | grep -q "$AUTO_ID" || die "the run ledger is empty: $ledger"
  ok "an automation ran and the ledger recorded it, with both attributions (#666)"

  # OpenBao secret persistence across a vault (and core) restart. *How* each runtime
  # restarts them is the caller's business — `docker compose restart` under compose,
  # deleting the pod and letting the unseal loop bring it back on Kubernetes — but the
  # property is identical: the secret is in the vault's storage, not in core's memory.
  http -X PUT "http://core-app:8080/platform/v1/llm/providers/claude/key" \
    -H 'Content-Type: application/json' -d '{"api_key":"ci-smoke-DO-NOT-USE"}' \
    | grep -q '"status":"ok"' || die "setting a provider key via core failed (OpenBao write/auth)"
  restart_openbao
  restart_core_app
  prov="$(http "http://core-app:8080/platform/v1/llm/providers")"
  printf '%s' "$prov" | grep -oE '"alias":"claude"[^}]*' | grep -q '"configured":true' \
    || die "provider key did not survive an OpenBao + core-app restart"
  ok "provider key survived a vault + core restart (persistent secrets)"
}

# The sign-in phase (#969). Runs LAST in each gate: it restarts core-app with sign-in on,
# and nothing after it may assume the web door is open. The runtime-specific half — *how*
# core-app is restarted with the new environment — is the gate's `enable_sign_in`: an
# override file merged onto the Compose service, `kubectl set env` on the Deployment.
smoke_assert_sign_in() {
  log "Asserting sign-in: AUTH_MODE=oidc behind the web door (#969)"
  enable_sign_in
  ok "core-app restarted with AUTH_MODE=oidc and an unreachable issuer (discovery is lazy)"

  # (1) The sign-in state through the web door. Polled: nginx re-resolves the core's address
  # on its resolver TTL (#891), so the first request after a recreate can still miss.
  i=0
  sess=""
  while [ "$i" -lt 30 ]; do
    sess="$(http "http://web:8080/platform/v1/auth/session" || true)"
    printf '%s' "$sess" | grep -q '"mode":"oidc"' && break
    i=$((i + 1))
    sleep 2
  done
  printf '%s' "$sess" | grep -q '"mode":"oidc"' \
    || die "the web door does not report AUTH_MODE=oidc (was core-app restarted with it?): $sess"
  printf '%s' "$sess" | grep -q '"signed_in":false' \
    || die "a cookie-less browser is reported as signed in: $sess"
  ok "GET /platform/v1/auth/session through the web door: oidc, signed out"

  # (2) The platform API through the web door needs a session now.
  code="$(http -o /dev/null -w '%{http_code}' "http://web:8080/platform/v1/modules" || true)"
  [ "$code" = "401" ] || die "the web door answered $code for the platform API with no session (expected 401)"
  body="$(http "http://web:8080/platform/v1/modules" || true)"
  printf '%s' "$body" | grep -q '"code":"unauthenticated"' \
    || die "the 401 is not the documented JSON: $body"
  ok "the platform API through the web door is 401 unauthenticated"

  # (3) A sign-in attempt against a provider that is down ends on the shell, never on a JSON
  # page: 302 to /?auth_error=provider_unreachable.
  hdrs="$(http -o /dev/null -D - "http://web:8080/platform/v1/auth/login?next=/chat" || true)"
  printf '%s' "$hdrs" | grep -qi '^location: /?auth_error=provider_unreachable' \
    || die "a sign-in with the provider down did not redirect with provider_unreachable: $hdrs"
  ok "a sign-in with the provider unreachable redirects to /?auth_error=provider_unreachable"

  # (4) Straight to the core — no proxy headers — the same route still answers: modules,
  # probes and Prometheus are inside the boundary and untouched by it.
  http -f "http://core-app:8080/platform/v1/modules" >/dev/null \
    || die "core-app refused a direct platform-API request with sign-in on (modules would break)"
  ok "the platform API directly on core-app still answers 200"

  # (5) Module <-> core, both directions: storage_list goes core -> storage over MCP, and the
  # storage module lists the file space by calling core's /platform/v1/files/list itself.
  # A file written directly beforehand must come back through that round trip.
  SIGNIN_FILE="smoke-sign-in-$$.txt"
  http -f -X PUT "http://core-app:8080/platform/v1/files/write?path=$SIGNIN_FILE" \
    -H 'Content-Type: application/json' -d '{"content":"sign-in phase"}' >/dev/null \
    || die "writing into the file space directly failed with sign-in on"
  sl="$(http -X POST "http://core-app:8080/platform/v1/modules/storage/tools/storage_list" \
    -H 'Content-Type: application/json' -d '{"arguments":{"path":""}}' || true)"
  printf '%s' "$sl" | grep -q "$SIGNIN_FILE" \
    || die "storage_list through core did not see the file (module -> core platform API refused?): $sl"
  ok "module <-> core traffic still works: storage_list round-trips through the platform API"
}
