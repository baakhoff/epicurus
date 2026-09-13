#!/usr/bin/env bash
# Restore epicurus from a backup created by backup.sh.
#
# Usage:
#   ./infra/backups/restore.sh <BACKUP_DIR>
#
# The script stops the application services (not infra) before restoring,
# then restarts the full stack. Postgres is restored via psql. Everything else
# is restored by extracting tar archives into the named volume — or, for the
# shared file space, into whatever EPICURUS_FILES_ROOT points at on THIS host,
# which need not be what it pointed at on the machine the backup came from.
#
# WARNING: This OVERWRITES the current contents. Run on a stopped or quiesced
# stack. The operator is responsible for stopping the stack first if live
# services are writing to the volumes.
#
# THIS IS THE DOCKER PATH; the Kubernetes runbook is still open as #895.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <BACKUP_DIR>" >&2
  exit 1
fi

BACKUP_DIR="$1"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROJECT="${COMPOSE_PROJECT_NAME:-epicurus}"

log() { echo "[restore] $*"; }

if [[ ! -f "${BACKUP_DIR}/manifest.json" ]]; then
  echo "ERROR: ${BACKUP_DIR}/manifest.json not found — is this a valid backup directory?" >&2
  exit 1
fi

log "Restoring from: ${BACKUP_DIR}"
log "Project: ${PROJECT}"
log ""
log "WARNING: This will overwrite the current data. Press Ctrl-C within 5 seconds to abort."
sleep 5

# --------------------------------------------------------------------------
# 1. Stop the stack (except infra services needed for Postgres restore).
# --------------------------------------------------------------------------
log "Stopping application services..."
# Derived from the compose `include:` list, never hand-kept: this list was two
# modules short (notes, messaging), and a module still running while its file space
# is wiped and re-extracted is exactly how a restore ends with a half-old tree.
mapfile -t APP_SERVICES < <(
  grep -oE 'services/[a-z0-9-]+/compose\.yaml' "${REPO_ROOT}/compose.yaml" |
    sed -E 's#services/([a-z0-9-]+)/.*#\1#' | sort -u
)
docker compose -f "${REPO_ROOT}/compose.yaml" \
  stop "${APP_SERVICES[@]}" 2>/dev/null || true

# --------------------------------------------------------------------------
# 2. Restore Postgres.
# --------------------------------------------------------------------------
if [[ -f "${BACKUP_DIR}/postgres.sql.gz" ]]; then
  log "Restoring Postgres..."
  # Drop all databases (except system ones) and re-create from dump.
  zcat "${BACKUP_DIR}/postgres.sql.gz" | \
    docker compose -f "${REPO_ROOT}/infra/compose/docker-compose.yml" \
      exec -T postgres \
      psql -U "${POSTGRES_USER:-epicurus}" postgres
  log "  Postgres restored."
else
  log "  (no postgres.sql.gz — skipping)"
fi

# --------------------------------------------------------------------------
# 3. Restore named volumes.
#
#    Exactly the set backup.sh archives — the two are held to each other, and to
#    the volumes the compose files declare, by tests/test_backup_volumes.py.
#    Postgres is restored above; the cache, the JetStream state and the
#    observability volumes are deliberately not in a backup at all (see backup.sh).
# --------------------------------------------------------------------------
VOLUMES=(openbao-data qdrant-data minio-data)

restore_volume() {
  local volume="$1"
  local name="${2:-$1}"
  local archive="${BACKUP_DIR}/${name}.tar.gz"
  if [[ ! -f "${archive}" ]]; then
    log "  (skipping ${name} — archive not found)"
    return
  fi
  log "Restoring volume ${volume} from ${name}.tar.gz..."
  # Wipe existing contents before extracting.
  docker run --rm \
    -v "${PROJECT}_${volume}:/data" \
    alpine sh -c "rm -rf /data/* /data/.[!.]*"
  docker run --rm \
    -v "${PROJECT}_${volume}:/data" \
    -v "${BACKUP_DIR}:/backup:ro" \
    alpine \
    tar xzf "/backup/${name}.tar.gz" -C /data
  log "  → restored ${volume}"
}

restore_host_dir() {
  local dir="$1"
  local name="$2"
  local archive="${BACKUP_DIR}/${name}.tar.gz"
  if [[ ! -f "${archive}" ]]; then
    log "  (skipping ${name} — archive not found)"
    return
  fi
  log "Restoring host directory ${dir} from ${name}.tar.gz..."
  mkdir -p "${dir}"
  # Through a container, as the backup was taken: root inside, so the archived
  # uids are restored even when the operator is not root on the host.
  docker run --rm \
    -v "${dir}:/data" \
    alpine sh -c "rm -rf /data/* /data/.[!.]*"
  docker run --rm \
    -v "${dir}:/data" \
    -v "${BACKUP_DIR}:/backup:ro" \
    alpine \
    tar xzf "/backup/${name}.tar.gz" -C /data
  log "  → restored ${dir}"
}

for vol in "${VOLUMES[@]}"; do
  restore_volume "${vol}"
done

# --------------------------------------------------------------------------
# 4. Restore the shared file space (ADR-0063).
#
#    Where it goes is decided by THIS host's EPICURUS_FILES_ROOT, not by the
#    machine the backup came from: moving a deployment from a named volume to a
#    bind mount (or the other way) is a normal thing to do, and a restore that
#    insisted on the old shape would refuse the one case it is most needed for.
#    backup.sh always names the archive epicurus-files.tar.gz for that reason.
# --------------------------------------------------------------------------
FILES_ROOT="${EPICURUS_FILES_ROOT:-}"
if [[ -z "${FILES_ROOT}" && -f "${REPO_ROOT}/.env" ]]; then
  FILES_ROOT="$(sed -n 's/^[[:space:]]*EPICURUS_FILES_ROOT=//p' "${REPO_ROOT}/.env" | tail -1)"
  FILES_ROOT="${FILES_ROOT%$'\r'}"
  FILES_ROOT="${FILES_ROOT%\"}"; FILES_ROOT="${FILES_ROOT#\"}"
  FILES_ROOT="${FILES_ROOT%\'}"; FILES_ROOT="${FILES_ROOT#\'}"
fi
if [[ "${FILES_ROOT}" == */* ]]; then
  restore_host_dir "${FILES_ROOT}" epicurus-files
else
  restore_volume "${FILES_ROOT:-epicurus-files}" epicurus-files
fi

# --------------------------------------------------------------------------
# 5. Restart the stack.
# --------------------------------------------------------------------------
log "Restarting the full stack..."
docker compose -f "${REPO_ROOT}/compose.yaml" up -d

log ""
log "Restore complete. The stack is starting — check Grafana in ~30 s."
log "If OpenBao is sealed after restore, the unseal sidecar will unseal it automatically."
