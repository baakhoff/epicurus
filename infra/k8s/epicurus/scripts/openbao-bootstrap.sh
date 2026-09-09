#!/usr/bin/env sh
# OpenBao bootstrap for the Kubernetes chart — the cluster twin of
# infra/compose/scripts/openbao-bootstrap.sh, and idempotent by construction so it
# can run as a post-install AND post-upgrade hook on every release.
#
# What it does, skipping whatever is already done:
#   1. waits for the OpenBao API;
#   2. initialises it (1-of-1 Shamir share) if it is uninitialised — after proving
#      it can write the Kubernetes Secret, and storing the unseal key + root token
#      in it straight away;
#   3. unseals it if it is sealed;
#   4. enables the KV v2 engine at `secret/`;
#   5. writes the `epicurus-core` policy;
#   6. mints a PERIODIC app token (768h period, renewed by core-app) unless the
#      Secret already holds one that still authenticates, and stores it;
#   7. optionally records the NATS role passwords in OpenBao as the source of
#      truth (best effort — the chart Secret already has them).
#
# The Secret it writes ($SECRET_NAME) is created through the Kubernetes API, not
# by Helm, so `helm uninstall` never deletes it: a reinstall over surviving PVCs
# still unseals. It holds `unseal-key`, `root-token` and `app-token`. BACK IT UP —
# without the unseal key the data on the OpenBao PVC is unreachable.
#
# Everything talks plain HTTP JSON (OpenBao's API and the Kubernetes API alike),
# so the image needs nothing but curl and a shell. Env:
#   BAO_ADDR      base URL of the OpenBao service          (required)
#   SECRET_NAME   Secret to create/patch                   (required)
#   NAMESPACE     namespace it lives in                    (required)
#   TENANT        tenant the NATS passwords are stored for (default: local)
#   STORE_NATS    "true" to run step 7                     (default: false)
#   K8S_API       Kubernetes API base URL   (default: https://kubernetes.default.svc)
#   SA_DIR        ServiceAccount token dir  (default: the projected in-pod path)
#
# The last two exist so tests/test_chart_services.py can run this whole script
# against a stub of both APIs — there is no cluster in CI, and this is the piece
# no amount of `helm template` can check.

set -eu

: "${BAO_ADDR:?BAO_ADDR must be set}"
: "${SECRET_NAME:?SECRET_NAME must be set}"
: "${NAMESPACE:?NAMESPACE must be set}"
TENANT="${TENANT:-local}"
STORE_NATS="${STORE_NATS:-false}"

SA_DIR="${SA_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"
K8S_API="${K8S_API:-https://kubernetes.default.svc}"
RESP=/tmp/response.json
CODE=""
BAO_TOKEN=""

# ── plumbing ──────────────────────────────────────────────────────────────────

# An empty X-Vault-Token header is harmless on the unauthenticated endpoints, so
# every call sends one and the token-vs-no-token branches collapse into this.
bao_req() { # method path [json-body]
    _method="$1"
    _path="$2"
    _body="${3:-}"
    if [ -n "$_body" ]; then
        CODE="$(curl -sS -o "$RESP" -w '%{http_code}' -X "$_method" \
            -H "X-Vault-Token: $BAO_TOKEN" \
            -H 'Content-Type: application/json' \
            --data-binary "$_body" \
            "$BAO_ADDR$_path")" || CODE="000"
    else
        CODE="$(curl -sS -o "$RESP" -w '%{http_code}' -X "$_method" \
            -H "X-Vault-Token: $BAO_TOKEN" \
            "$BAO_ADDR$_path")" || CODE="000"
    fi
}

k8s_req() { # method path content-type [json-body]
    _method="$1"
    _path="$2"
    _ctype="$3"
    _body="${4:-}"
    if [ -n "$_body" ]; then
        CODE="$(curl -sS -o "$RESP" -w '%{http_code}' -X "$_method" \
            --cacert "$SA_DIR/ca.crt" \
            -H "Authorization: Bearer $(cat "$SA_DIR/token")" \
            -H "Content-Type: $_ctype" \
            --data-binary "$_body" \
            "$K8S_API$_path")" || CODE="000"
    else
        CODE="$(curl -sS -o "$RESP" -w '%{http_code}' -X "$_method" \
            --cacert "$SA_DIR/ca.crt" \
            -H "Authorization: Bearer $(cat "$SA_DIR/token")" \
            "$K8S_API$_path")" || CODE="000"
    fi
}

# Both APIs answer with compact single-line JSON, so a targeted sed is enough —
# the same trade the compose bootstrap script makes rather than shipping jq.
json_string() { # key -> first matching string value in $RESP
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$RESP" | head -n 1
}

json_array_first() { # key -> first string element of the array at that key in $RESP
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\[[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$RESP" | head -n 1
}

b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
unb64() { printf '%s' "$1" | base64 -d; }

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

# ── 1. wait for the API ───────────────────────────────────────────────────────

echo "=== OpenBao bootstrap (namespace $NAMESPACE) ==="
attempt=0
while true; do
    bao_req GET /v1/sys/seal-status
    if [ "$CODE" = "200" ]; then
        break
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        fail "OpenBao at $BAO_ADDR never answered (last HTTP $CODE)"
    fi
    echo "  waiting for OpenBao at $BAO_ADDR (HTTP $CODE)..."
    sleep 5
done
# Tolerant of whitespace: OpenBao answers compact, but nothing in the API contract
# promises it, and a lone space would otherwise silently read as "not sealed".
SEALED="$(grep -Ec '"sealed"[[:space:]]*:[[:space:]]*true' "$RESP" || true)"
INITIALIZED="$(grep -Ec '"initialized"[[:space:]]*:[[:space:]]*true' "$RESP" || true)"
echo "  reachable (initialized=$INITIALIZED sealed=$SEALED)"

# ── read whatever a previous run stored ───────────────────────────────────────

UNSEAL_KEY=""
ROOT_TOKEN=""
APP_TOKEN=""
k8s_req GET "/api/v1/namespaces/$NAMESPACE/secrets/$SECRET_NAME" application/json
if [ "$CODE" = "200" ]; then
    _k="$(json_string 'unseal-key')"
    _r="$(json_string 'root-token')"
    _a="$(json_string 'app-token')"
    if [ -n "$_k" ]; then UNSEAL_KEY="$(unb64 "$_k")"; fi
    if [ -n "$_r" ]; then ROOT_TOKEN="$(unb64 "$_r")"; fi
    if [ -n "$_a" ]; then APP_TOKEN="$(unb64 "$_a")"; fi
    echo "  found the existing $SECRET_NAME secret"
elif [ "$CODE" != "404" ]; then
    fail "could not read secret/$SECRET_NAME (HTTP $CODE)"
fi

store_secret() { # key value — create the Secret, or merge this key into it
    _key="$1"
    _val="$(b64 "$2")"
    k8s_req PATCH "/api/v1/namespaces/$NAMESPACE/secrets/$SECRET_NAME" \
        application/merge-patch+json "{\"data\":{\"$_key\":\"$_val\"}}"
    if [ "$CODE" = "404" ]; then
        k8s_req POST "/api/v1/namespaces/$NAMESPACE/secrets" application/json \
            "{\"apiVersion\":\"v1\",\"kind\":\"Secret\",\"metadata\":{\"name\":\"$SECRET_NAME\",\"labels\":{\"app.kubernetes.io/part-of\":\"epicurus\",\"app.kubernetes.io/component\":\"openbao\"}},\"type\":\"Opaque\",\"data\":{\"$_key\":\"$_val\"}}"
    fi
    case "$CODE" in
        200 | 201) ;;
        *) fail "could not store '$_key' in secret/$SECRET_NAME (HTTP $CODE)" ;;
    esac
}

# ── 2. initialise ─────────────────────────────────────────────────────────────

if [ "$INITIALIZED" = "0" ]; then
    # Prove we can WRITE the Secret *before* creating a vault. An operator-supplied
    # ServiceAccount, an admission policy or a resource quota can withhold
    # create/patch while still allowing the `get` above — and if the store fails
    # after init, the only unseal key that ever existed dies with this pod and the
    # data on the volume is unreachable. One throwaway key closes that window.
    echo "Checking that this job can write secret/$SECRET_NAME..."
    store_secret bootstrap-probe "ok"

    echo "Initialising (1-of-1 Shamir shares)..."
    bao_req POST /v1/sys/init '{"secret_shares":1,"secret_threshold":1}'
    [ "$CODE" = "200" ] || fail "init failed (HTTP $CODE)"
    # The HTTP API returns `keys` (hex) and `keys_base64`. The *CLI*
    # (`bao operator init -format=json`, which the compose bootstrap parses) calls
    # the same field `unseal_keys_b64` — and reading the CLI's name off an HTTP
    # response is what made the chart's very first real boot initialise a vault and
    # then throw away the only key that could ever open it (#894, unrecoverable:
    # every retry then found an initialised vault with no stored key). Read the API's
    # name, and accept the CLI's as a fallback so neither shape can wedge a vault.
    UNSEAL_KEY="$(json_array_first keys_base64)"
    [ -n "$UNSEAL_KEY" ] || UNSEAL_KEY="$(json_array_first unseal_keys_b64)"
    ROOT_TOKEN="$(json_string 'root_token')"
    [ -n "$UNSEAL_KEY" ] || fail "init response carried no unseal key"
    [ -n "$ROOT_TOKEN" ] || fail "init response carried no root token"
    # Store both BEFORE anything else can fail: an unseal key that only ever
    # existed in this pod's memory would leave the volume unreadable forever.
    store_secret unseal-key "$UNSEAL_KEY"
    store_secret root-token "$ROOT_TOKEN"
    APP_TOKEN=""
    SEALED="1"
    echo "  initialised; unseal key and root token stored in secret/$SECRET_NAME"
else
    echo "Already initialised."
    [ -n "$UNSEAL_KEY" ] || fail \
        "OpenBao is initialised but secret/$SECRET_NAME holds no unseal key — restore that Secret from your backup, or delete the OpenBao PVC to start over (this destroys stored secrets)"
fi

# ── 3. unseal ─────────────────────────────────────────────────────────────────

if [ "$SEALED" != "0" ]; then
    echo "Unsealing..."
    bao_req POST /v1/sys/unseal "{\"key\":\"$UNSEAL_KEY\"}"
    [ "$CODE" = "200" ] || fail "unseal failed (HTTP $CODE)"
    grep -Eq '"sealed"[[:space:]]*:[[:space:]]*false' "$RESP" || fail "still sealed after submitting the key"
    echo "  unsealed."
fi

[ -n "$ROOT_TOKEN" ] || fail "secret/$SECRET_NAME holds no root token; cannot configure the vault"
BAO_TOKEN="$ROOT_TOKEN"

# ── 4. KV v2 at secret/ ───────────────────────────────────────────────────────

echo "Enabling the KV v2 engine at secret/..."
bao_req POST /v1/sys/mounts/secret '{"type":"kv","options":{"version":"2"}}'
case "$CODE" in
    200 | 204) echo "  enabled." ;;
    400) echo "  (already enabled)" ;;
    *) fail "could not enable the KV engine (HTTP $CODE)" ;;
esac

# ── 5. the epicurus-core policy ───────────────────────────────────────────────
#
# Identical to the compose bootstrap's policy. The self-management paths matter:
# the app token is minted with no default policy, and core-app's SecretStore does
# a token lookup-self before every connection and renews the lease daily (#728).

echo "Writing the epicurus-core policy..."
POLICY='path \"secret/data/tenants/*\" {\n  capabilities = [\"create\", \"read\", \"update\", \"delete\", \"list\"]\n}\npath \"secret/metadata/tenants/*\" {\n  capabilities = [\"list\", \"delete\"]\n}\npath \"auth/token/lookup-self\" {\n  capabilities = [\"read\"]\n}\npath \"auth/token/renew-self\" {\n  capabilities = [\"update\"]\n}\n'
bao_req PUT /v1/sys/policies/acl/epicurus-core "{\"policy\":\"$POLICY\"}"
case "$CODE" in
    200 | 204) echo "  written." ;;
    *) fail "could not write the epicurus-core policy (HTTP $CODE)" ;;
esac

# ── 6. the periodic app token ─────────────────────────────────────────────────
#
# PERIODIC is the load-bearing word (#728): a plain service token silently falls
# back to the 768h system default lease and every secret read starts 403ing about
# a month after bootstrap. A periodic token's lease is always renewable back to
# the full period, and core-app renews it daily.

REUSE_TOKEN="0"
if [ -n "$APP_TOKEN" ]; then
    BAO_TOKEN="$APP_TOKEN"
    bao_req GET /v1/auth/token/lookup-self
    if [ "$CODE" = "200" ]; then
        REUSE_TOKEN="1"
    fi
    BAO_TOKEN="$ROOT_TOKEN"
fi

if [ "$REUSE_TOKEN" = "1" ]; then
    echo "Existing app token still authenticates — keeping it."
else
    if [ -n "$APP_TOKEN" ]; then
        # Best effort: the old token normally failed lookup-self precisely because
        # it is already gone, but if it failed for some other reason it is orphaned
        # AND periodic — an unlimited lifetime over secret/data/tenants/* that
        # nothing would ever clean up. Re-minting on every upgrade would accumulate
        # them.
        echo "Revoking the superseded app token..."
        bao_req POST /v1/auth/token/revoke "{\"token\":\"$APP_TOKEN\"}"
        echo "  (HTTP $CODE)"
    fi
    echo "Creating the periodic app token (768h period, renewed by core-app)..."
    bao_req POST /v1/auth/token/create \
        '{"display_name":"epicurus-core-app","policies":["epicurus-core"],"no_default_policy":true,"no_parent":true,"period":"768h"}'
    [ "$CODE" = "200" ] || fail "could not create the app token (HTTP $CODE)"
    APP_TOKEN="$(json_string 'client_token')"
    [ -n "$APP_TOKEN" ] || fail "token response carried no client_token"
    store_secret app-token "$APP_TOKEN"
    echo "  stored in secret/$SECRET_NAME (app pods mount it at OPENBAO_TOKEN_FILE)"
fi

# ── 7. NATS role passwords (best effort) ──────────────────────────────────────

if [ "$STORE_NATS" = "true" ]; then
    echo "Recording the NATS role passwords in OpenBao..."
    bao_req POST "/v1/secret/data/tenants/$TENANT/nats" \
        "{\"data\":{\"core\":\"${NATS_CORE_PASSWORD:-}\",\"module\":\"${NATS_MODULE_PASSWORD:-}\",\"sys\":\"${NATS_SYS_PASSWORD:-}\"}}"
    case "$CODE" in
        200 | 204) echo "  stored at secret/tenants/$TENANT/nats" ;;
        *) echo "  (warning: could not store them, HTTP $CODE — the chart Secret still has them)" ;;
    esac
fi

echo "=== Bootstrap complete ==="
