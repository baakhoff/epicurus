#!/usr/bin/env sh
# Kubernetes smoke gate (#894) — install the Helm chart into a real cluster and
# assert the same integration last mile the Compose gate asserts, plus the two
# things only a cluster can prove.
#
# Until this existed the chart was rendered and schema-checked by `chart-validate`
# and never booted (ADR-0135's own "Consequences" says so), and the Kubernetes arm
# of the container-runtime seam (#891, ADR-0134) had only ever met an httpx mock
# transport. Both gaps close here: the chart comes up on kind, and the core scales
# a real module Deployment to zero through the scoped Role the chart renders.
#
# The runtime-neutral assertions live in infra/ci/smoke-assert.sh and are shared
# verbatim with infra/ci/smoke.sh, so the two gates cannot drift. This script owns
# the Kubernetes-specific half: build + load the images, install the chart, reach
# the services through a curl pod in the namespace (the symmetric twin of the
# Compose gate's throwaway curl container), restart workloads, and diagnose.
#
#   sh infra/ci/k8s-smoke.sh                      # create a cluster, boot, assert, delete
#   KEEP_UP=1 sh infra/ci/k8s-smoke.sh            # leave the cluster up to poke at
#   SMOKE_SKIP_BUILD=1 sh infra/ci/k8s-smoke.sh   # images already built and loaded
#
# In CI the cluster is created by helm/kind-action before this runs; an existing
# cluster of the same name is reused and never deleted by this script.
set -eu

# shellcheck disable=SC1007 # intentional: clears CDPATH so `cd` can't print an unexpected path
ROOT="$(CDPATH= cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

CLUSTER="${KIND_CLUSTER:-epicurus-ci}"
NS="${K8S_SMOKE_NAMESPACE:-epicurus}"
RELEASE="${K8S_SMOKE_RELEASE:-epicurus}"
CHART="infra/k8s/epicurus"
VALUES="infra/ci/values-ci.yaml"
# The chart's own registry/prefix with a CI tag, so the rendered image refs are the
# real ones and only the tag is local (see infra/ci/values-ci.yaml).
IMAGE_PREFIX="ghcr.io/baakhoff/epicurus-"
IMAGE_TAG="${K8S_SMOKE_IMAGE_TAG:-ci}"
CURL_IMG="curlimages/curl:8.11.1"
CURL_POD="smoke-curl"
CREATED_CLUSTER=0

# The runtime-neutral assertions, shared with the Compose gate. Sourced early
# because it also owns `smoke_modules`, the canonical module set both gates boot.
# shellcheck source=infra/ci/smoke-assert.sh disable=SC1091 # linted on its own; CI lints one file at a time
. "$ROOT/infra/ci/smoke-assert.sh"

EXPECT_MODULES="$(smoke_modules)"
# Every first-party image the chart deploys. Derived from the Dockerfiles, exactly
# as the `images` CI job does, so a new service is built and loaded with no edit.
SERVICES="$(
  for df in services/*/Dockerfile; do
    basename "$(dirname "$df")"
  done | sort | tr '\n' ' '
)"

# ── output helpers ────────────────────────────────────────────────────────────
log() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
ok()  { printf '  \033[1;32mPASS\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mK8S SMOKE FAILED: %s\033[0m\n' "$*" >&2; exit 1; }

kc() { kubectl -n "$NS" "$@"; }

# HTTP from inside the cluster network — no port-forward, no NodePort, no ingress.
# The curl pod is the namespace-local twin of the Compose gate's curl container:
# it reaches every Service by its bare name, which is exactly how the core, the
# modules and the web shell reach each other.
http() { kc exec "$CURL_POD" -- curl -s --max-time 25 "$@"; }

# ── the restart callbacks smoke-assert.sh calls (Kubernetes' answer) ──────────
restart_openbao() { # delete the vault pod; the chart's unseal loop brings it back
  kc delete pod openbao-0 --wait=true >/dev/null 2>&1 || true
  kc rollout status statefulset/openbao --timeout=180s >/dev/null
  # A restarted vault comes back SEALED, and only the unseal Deployment can fix
  # that — it polls on an interval, so this waits for the *unsealed* state rather
  # than for the pod. `/v1/sys/health` is 200 only when initialised and unsealed.
  i=0
  while [ "$i" -lt 60 ]; do
    http -f "http://openbao:8200/v1/sys/health" >/dev/null 2>&1 && return 0
    i=$((i + 1))
    sleep 3
  done
  die "OpenBao never came back unsealed after a pod restart (the unseal loop?)"
}

restart_core_app() {
  kc rollout restart deployment/core-app >/dev/null
  kc rollout status deployment/core-app --timeout=300s >/dev/null
}

dump_diagnostics() {
  log "Diagnostics (k8s smoke failed)"
  kc get pods -o wide 2>&1 || true
  kc get pvc 2>&1 || true
  kc get jobs 2>&1 || true
  printf '\n--- recent events ---\n'
  kc get events --sort-by=.lastTimestamp 2>&1 | tail -40 || true
  # Anything not Running/Completed gets a describe — that is where an
  # ImagePullBackOff, an unschedulable pod or a failing probe explains itself.
  for p in $(kc get pods -o jsonpath='{range .items[?(@.status.phase!="Running")]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
    printf '\n--- describe: %s ---\n' "$p"
    kc describe "pod/$p" 2>&1 | tail -40 || true
  done
  for c in openbao openbao-unseal openbao-bootstrap core-app web $EXPECT_MODULES; do
    printf '\n--- logs: %s ---\n' "$c"
    kc logs --tail=40 --all-containers=true \
      -l "app.kubernetes.io/component=$c" 2>&1 || true
  done
}

cleanup() {
  rc=$?
  [ "$rc" -ne 0 ] && dump_diagnostics
  if [ "${KEEP_UP:-0}" = "1" ]; then
    log "KEEP_UP=1 — leaving cluster '$CLUSTER' up (tear down: kind delete cluster --name $CLUSTER)"
  elif [ "$CREATED_CLUSTER" = "1" ]; then
    log "Deleting the kind cluster"
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  else
    # CI's helm/kind-action deletes the cluster it created in its own post step.
    log "Leaving cluster '$CLUSTER' to whoever created it"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── cluster ───────────────────────────────────────────────────────────────────
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "Reusing the existing kind cluster '$CLUSTER'"
else
  log "Creating kind cluster '$CLUSTER'"
  kind create cluster --name "$CLUSTER" --wait 180s
  CREATED_CLUSTER=1
fi
kubectl cluster-info --context "kind-$CLUSTER" >/dev/null \
  || die "cannot reach the kind cluster '$CLUSTER'"
kubectl config use-context "kind-$CLUSTER" >/dev/null
kubectl version -o yaml 2>/dev/null | grep -E '^\s+gitVersion' || true

# ── images ────────────────────────────────────────────────────────────────────
if [ "${SMOKE_SKIP_BUILD:-0}" != "1" ]; then
  IMAGES=""
  for svc in $SERVICES; do
    img="${IMAGE_PREFIX}${svc}:${IMAGE_TAG}"
    log "Building $img"
    docker build -f "services/$svc/Dockerfile" -t "$img" .
    IMAGES="$IMAGES $img"
  done
  log "Loading the images into the cluster"
  # One invocation: kind exports them all into a single archive, so the layers the
  # images share (the Python base, epicurus-core) are transferred once.
  # shellcheck disable=SC2086 # $IMAGES is a deliberate space-separated ref list
  kind load docker-image --name "$CLUSTER" $IMAGES
  ok "built and loaded $(printf '%s' "$SERVICES" | wc -w | tr -d ' ') service images"
fi

# ── install ───────────────────────────────────────────────────────────────────
log "Installing the chart (helm install --wait)"
helm install "$RELEASE" "$CHART" \
  --namespace "$NS" --create-namespace \
  --values "$VALUES" \
  --set "image.tag=$IMAGE_TAG" \
  --wait --timeout 10m

# `helm --wait` waits for workloads, not for Jobs, and core-app's init container
# waits for the token this Job writes — so a ready core already implies it ran.
# Assert it anyway: a Job that failed and was retried into success is worth seeing.
kc wait --for=condition=complete job \
  -l app.kubernetes.io/component=openbao-bootstrap --timeout=300s >/dev/null
ok "the OpenBao bootstrap Job completed"

unseal_ready="$(kc get deployment openbao-unseal -o jsonpath='{.status.availableReplicas}' 2>/dev/null || true)"
[ "$unseal_ready" = "1" ] || die "openbao-unseal has $unseal_ready available replicas (expected 1)"
ok "the unseal loop is running (not crash-looping on a missing key Secret)"

log "Starting the in-namespace curl pod"
kc run "$CURL_POD" --image="$CURL_IMG" --restart=Never --command -- sleep 3600 >/dev/null
kc wait --for=condition=Ready "pod/$CURL_POD" --timeout=180s >/dev/null
ok "curl pod ready"

# ── Kubernetes-only assertions ────────────────────────────────────────────────
log "Asserting the Kubernetes-specific last mile"

# The app token must be *periodic* (#728) here too — the chart's bootstrap script
# is a separate implementation from the compose one, so the property has to be
# asserted separately. It is read through the token's own `lookup-self`, which is
# the only token capability the epicurus-core policy grants beyond secret/.
app_token="$(kc get secret epicurus-openbao -o jsonpath='{.data.app-token}' | base64 -d)"
[ -n "$app_token" ] || die "the bootstrap Job wrote no app token into the epicurus-openbao Secret"
token_period="$(http -H "X-Vault-Token: $app_token" \
  "http://openbao:8200/v1/auth/token/lookup-self" | tr -d ' \t\r\n' \
  | grep -o '"period":[0-9]*' | cut -d: -f2)"
case "$token_period" in
  ''|*[!0-9]*|0) die "app token is not periodic (period='${token_period:-unset}') — it expires, see #728" ;;
esac
ok "app token is periodic (${token_period}s), so renewal can keep it alive indefinitely"

# The web shell derives nginx's resolver from the pod's own /etc/resolv.conf
# (#891): the hardcoded 127.0.0.11 it used before is Docker's embedded DNS and
# does not exist in a pod, so every proxied request would fail at name resolution
# while both probes stayed green. Only a real cluster can prove this.
http -f "http://web:8080/healthz" >/dev/null || die "web /healthz unreachable or non-200"
winfo="$(http "http://web:8080/platform/v1/info" || true)"
printf '%s' "$winfo" | grep -q '"core_app_version"' \
  || die "web did not proxy /platform/ to the core (nginx resolver wrong for a pod?): $winfo"
ok "the web shell proxies /platform/ to the core through the pod's own DNS (#891)"

# ── the runtime-neutral last mile, shared with the Compose gate ────────────────
smoke_assert

# ── the Kubernetes arm of the container-runtime seam (#891, ADR-0134) ──────────
# Everything above is true of any deployment. This is the part that was mock-only
# until now: in a pod there is no Docker daemon, so `CONTAINER_RUNTIME=auto` must
# resolve to `kubernetes`, and a confirmed module removal must scale that module's
# Deployment to zero through the namespace-scoped Role the chart renders.
log "Asserting the Kubernetes container-runtime seam"

ds="$(http "http://core-app:8080/platform/v1/modules/docker-status" || true)"
printf '%s' "$ds" | grep -q '"available":true' \
  || die "container control is unavailable in-cluster (CONTAINER_RUNTIME=auto did not resolve, or no namespace): $ds"
# The status contract is still Docker-worded and carries no runtime name (a known
# #891 follow-up), so the *identity* of the selected arm is read from the one line
# KubernetesController.from_env logs at startup.
kc logs --tail=-1 -l app.kubernetes.io/component=core-app 2>/dev/null \
  | grep -q 'kubernetes control ready' \
  || die "core-app did not select the Kubernetes container-runtime arm (CONTAINER_RUNTIME=auto detection broken?)"
ok "CONTAINER_RUNTIME=auto resolved to the Kubernetes arm inside the pod (#891)"

# `echo` last, and only here: removal tombstones the module for good, so nothing
# after this may need it. Deferred=true would mean the Role was refused.
rm_body="$(http -X DELETE "http://core-app:8080/platform/v1/modules/echo" || true)"
printf '%s' "$rm_body" | grep -q '"removed":"echo"' || die "removing the echo module failed: $rm_body"
printf '%s' "$rm_body" | grep -q '"container_teardown_deferred":false' \
  || die "the scale-down was deferred — the chart's Role did not permit deployments/scale: $rm_body"
printf '%s' "$rm_body" | grep -q '"containers":1' \
  || die "expected exactly one Deployment scaled down, got: $rm_body"
replicas="$(kc get deployment echo -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
[ "$replicas" = "0" ] || die "echo's Deployment has $replicas replicas after removal (expected 0)"
ok "a confirmed removal scaled the module's Deployment to zero through the scoped Role (#891)"

log "ALL K8S SMOKE CHECKS PASSED"
