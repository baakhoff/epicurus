#!/usr/bin/env sh
# Bootstrap script for OpenBao persistent mode.
# Run ONCE after the first `docker compose up` (or `task infra-up`).
#
# What it does:
#   1. Initialises OpenBao (1-of-1 Shamir share) if not already done.
#   2. Unseals OpenBao using the generated key.
#   3. Enables the KV v2 secrets engine on the `secret/` mount.
#   4. Creates the `epicurus-core` policy (full tenant-scoped KV access).
#   5. Creates a non-expiring app token for the core service.
#   6. Writes OPENBAO_UNSEAL_KEY and OPENBAO_TOKEN to infra/compose/.env.secrets.
#
# After running:
#   - Add OPENBAO_UNSEAL_KEY and OPENBAO_TOKEN from .env.secrets to your .env.
#   - The openbao-unseal container reads OPENBAO_UNSEAL_KEY from .env on every
#     stack restart and automatically unseals the vault.
#   - Keep .env.secrets safe; it holds the single unseal key and the root token.
#
# Usage:
#   sh infra/compose/scripts/openbao-bootstrap.sh
# or, targeting a non-default OpenBao address:
#   BAO_ADDR=http://localhost:8200 sh infra/compose/scripts/openbao-bootstrap.sh
#
# The script runs the `bao` CLI via `docker compose exec` — no local `bao`
# installation needed. Set COMPOSE_FILE if you are not running from the repo root.

set -e

COMPOSE_FILE="${COMPOSE_FILE:-infra/compose/docker-compose.yml}"
SECRETS_FILE="${SECRETS_FILE:-infra/compose/.env.secrets}"

BAO() {
    # Forward BAO_TOKEN into the container — `docker compose exec` does NOT inherit
    # the host shell's env, so the `BAO_TOKEN=… BAO …` prefixes on the authenticated
    # calls below would otherwise run tokenless (403). Empty for the pre-auth calls.
    docker compose -f "$COMPOSE_FILE" exec -T -e BAO_TOKEN="${BAO_TOKEN:-}" openbao bao "$@"
}

# ── Wait for OpenBao API ──────────────────────────────────────────────────────
echo "=== OpenBao bootstrap ==="
echo ""
printf "Waiting for OpenBao to be reachable"
i=0
while true; do
    # `|| s=$?` keeps `set -e` from killing us on a sealed vault (bao status exits 2).
    s=0; BAO status > /dev/null 2>&1 || s=$?
    [ "$s" -ne 1 ] && break   # exit 0 = active, 2 = sealed — both mean "running"
    i=$((i + 1))
    [ $i -ge 30 ] && { echo ""; echo "Timed out waiting for OpenBao"; exit 1; }
    printf "."
    sleep 2
done
echo " ready."

# ── 1. Initialise ─────────────────────────────────────────────────────────────
init_json=$(BAO operator init -status -format=json 2>/dev/null || echo '{"initialized":false}')
initialized=$(printf '%s' "$init_json" | grep -o '"initialized":[^,}]*' | cut -d: -f2 | tr -d ' "')

if [ "$initialized" = "false" ]; then
    echo "Initialising (1-of-1 Shamir shares)..."
    init_out=$(BAO operator init -key-shares=1 -key-threshold=1 -format=json)
    # `-format=json` pretty-prints across multiple lines; flatten whitespace so the
    # grep/sed below match (b64 keys / tokens never contain spaces, so this is safe).
    init_out=$(printf '%s' "$init_out" | tr -d ' \t\r\n')

    # Use the OPENBAO_* names the unseal/policy steps below read (and that the
    # else-branch sources from the file) — not bare UNSEAL_KEY/ROOT_TOKEN.
    OPENBAO_UNSEAL_KEY=$(printf '%s' "$init_out" | grep -o '"unseal_keys_b64":\["[^"]*"' | sed 's/.*\["\(.*\)"/\1/')
    OPENBAO_ROOT_TOKEN=$(printf '%s' "$init_out" | grep -o '"root_token":"[^"]*"' | cut -d'"' -f4)

    mkdir -p "$(dirname "$SECRETS_FILE")"
    {
        printf '# OpenBao bootstrap secrets — NEVER commit this file.\n'
        printf 'OPENBAO_UNSEAL_KEY=%s\n' "$OPENBAO_UNSEAL_KEY"
        printf 'OPENBAO_ROOT_TOKEN=%s\n' "$OPENBAO_ROOT_TOKEN"
    } > "$SECRETS_FILE"
    chmod 600 "$SECRETS_FILE"
    echo "Init secrets written to $SECRETS_FILE"
else
    echo "Already initialised — reading secrets from $SECRETS_FILE"
    if [ ! -f "$SECRETS_FILE" ]; then
        echo "ERROR: $SECRETS_FILE not found; cannot unseal. Recreate the volume and re-run."
        exit 1
    fi
    # shellcheck disable=SC1090
    . "$SECRETS_FILE"
fi

# ── 2. Unseal ─────────────────────────────────────────────────────────────────
echo "Unsealing..."
BAO operator unseal "$OPENBAO_UNSEAL_KEY"

# ── 3. KV v2 engine ───────────────────────────────────────────────────────────
echo "Enabling KV v2 secrets engine at secret/..."
BAO_TOKEN="$OPENBAO_ROOT_TOKEN" BAO secrets enable -version=2 -path=secret kv 2>/dev/null \
    || echo "  (already enabled)"

# ── 4. epicurus-core policy ────────────────────────────────────────────────────
echo "Writing epicurus-core policy..."
BAO_TOKEN="$OPENBAO_ROOT_TOKEN" BAO policy write epicurus-core - <<'POLICY'
path "secret/data/tenants/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/tenants/*" {
  capabilities = ["list", "delete"]
}
# The app token is created with -no-default-policy, so grant the self-management
# paths the default policy would have. core-app's SecretStore calls hvac's
# is_authenticated() (a token lookup-self) before every connection; without this
# the token works for KV but the auth preflight 403s and core-app refuses to start.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
POLICY

# ── 5. App token (non-expiring) ────────────────────────────────────────────────
echo "Creating non-expiring app token..."
token_json=$(BAO_TOKEN="$OPENBAO_ROOT_TOKEN" BAO token create \
    -display-name=epicurus-core-app \
    -policy=epicurus-core \
    -no-default-policy \
    -explicit-max-ttl=0 \
    -format=json)
# Flatten the pretty-printed JSON (as with init above) before extracting the token.
token_json=$(printf '%s' "$token_json" | tr -d ' \t\r\n')

APP_TOKEN=$(printf '%s' "$token_json" | grep -o '"client_token":"[^"]*"' | cut -d'"' -f4)

printf 'OPENBAO_TOKEN=%s\n' "$APP_TOKEN" >> "$SECRETS_FILE"

# ── 6. NATS role credentials (#50, ADR-0066) ───────────────────────────────────
# The authenticated bus (infra/compose/nats-server.conf) needs one password per role
# (core / module / sys). Generate strong ones, record them in OpenBao as the source
# of truth, and emit them to the secrets file — the SAME value feeds the nats server
# AND every service that authenticates against it (compose maps each of
# NATS_{CORE,MODULE,SYS}_PASSWORD to both sides). This replaces the weak `epicurus-dev`
# compose defaults that are only safe on a private dev box.
echo "Generating NATS role passwords..."
nats_pw() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \t\r\n'; }
NATS_CORE_PASSWORD=$(nats_pw)
NATS_MODULE_PASSWORD=$(nats_pw)
NATS_SYS_PASSWORD=$(nats_pw)

# Source of truth in OpenBao, tenant-scoped (constraint #1; the policy grants
# secret/data/tenants/*). A failure here is non-fatal — the secrets file still has them.
TENANT="${DEFAULT_TENANT_ID:-local}"
if BAO_TOKEN="$OPENBAO_ROOT_TOKEN" BAO kv put "secret/tenants/$TENANT/nats" \
    core="$NATS_CORE_PASSWORD" module="$NATS_MODULE_PASSWORD" sys="$NATS_SYS_PASSWORD" \
    > /dev/null 2>&1; then
    echo "  stored at secret/tenants/$TENANT/nats"
else
    echo "  (warning: could not store NATS passwords in OpenBao; the secrets file still has them)"
fi

{
    printf 'NATS_CORE_PASSWORD=%s\n'   "$NATS_CORE_PASSWORD"
    printf 'NATS_MODULE_PASSWORD=%s\n' "$NATS_MODULE_PASSWORD"
    printf 'NATS_SYS_PASSWORD=%s\n'    "$NATS_SYS_PASSWORD"
} >> "$SECRETS_FILE"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Bootstrap complete ==="
echo ""
echo "Add these lines to your .env (or: source $SECRETS_FILE):"
echo ""
printf '  OPENBAO_UNSEAL_KEY=%s\n'   "$OPENBAO_UNSEAL_KEY"
printf '  OPENBAO_TOKEN=%s\n'        "$APP_TOKEN"
printf '  NATS_CORE_PASSWORD=%s\n'   "$NATS_CORE_PASSWORD"
printf '  NATS_MODULE_PASSWORD=%s\n' "$NATS_MODULE_PASSWORD"
printf '  NATS_SYS_PASSWORD=%s\n'    "$NATS_SYS_PASSWORD"
echo ""
echo "The openbao-unseal container uses OPENBAO_UNSEAL_KEY to auto-unseal on"
echo "every stack restart. The NATS_* passwords authenticate the core/modules to the"
echo "bus — set the same values for the nats service and the services. Keep"
echo "$SECRETS_FILE safe."
