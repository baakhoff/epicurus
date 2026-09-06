#!/usr/bin/env sh
# Keeps OpenBao unsealed — the cluster twin of the compose `openbao-unseal`
# sidecar. A restarted OpenBao pod comes back SEALED, and a sealed vault means
# every secret read fails, so something has to submit the key on every restart.
#
# The key comes from the Secret the bootstrap Job writes, mounted as a file with
# `optional: true`: on a first install this loop starts before that Secret exists,
# waits for the file to appear (the kubelet syncs it in without a restart), and
# then behaves exactly like the compose sidecar.
#
# Env:
#   BAO_ADDR          base URL of the OpenBao service   (required)
#   UNSEAL_KEY_FILE   path to the mounted key           (required)
#   INTERVAL_SECONDS  pause between checks              (default: 30)

set -eu

: "${BAO_ADDR:?BAO_ADDR must be set}"
: "${UNSEAL_KEY_FILE:?UNSEAL_KEY_FILE must be set}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-30}"

STATUS=/tmp/seal-status.json

while true; do
    if [ ! -s "$UNSEAL_KEY_FILE" ]; then
        echo "waiting for the bootstrap job to write the unseal key..."
        sleep 10
        continue
    fi

    CODE="$(curl -sS -o "$STATUS" -w '%{http_code}' "$BAO_ADDR/v1/sys/seal-status")" || CODE="000"
    if [ "$CODE" != "200" ]; then
        echo "OpenBao not reachable (HTTP $CODE), retrying..."
        sleep 5
        continue
    fi

    # An uninitialised vault also reports sealed; submitting a key there is a 400.
    # Leave that case to the bootstrap job.
    if grep -Eq '"initialized"[[:space:]]*:[[:space:]]*true' "$STATUS" &&
        grep -Eq '"sealed"[[:space:]]*:[[:space:]]*true' "$STATUS"; then
        echo "Sealed — unsealing..."
        KEY="$(cat "$UNSEAL_KEY_FILE")"
        CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
            -H 'Content-Type: application/json' \
            --data-binary "{\"key\":\"$KEY\"}" \
            "$BAO_ADDR/v1/sys/unseal")" || CODE="000"
        if [ "$CODE" = "200" ]; then
            echo "Unsealed."
        else
            echo "Unseal attempt failed (HTTP $CODE)."
        fi
    fi

    sleep "$INTERVAL_SECONDS"
done
