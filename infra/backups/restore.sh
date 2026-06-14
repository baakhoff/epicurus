#!/usr/bin/env bash
# Restore epicurus volumes from a backup created by backup.sh.
#
# Usage:
#   ./infra/backups/restore.sh <BACKUP_DIR>
#
# The script stops the application services (not infra) before restoring,
# then restarts the full stack. Postgres is restored via psql. Other volumes
# are restored by extracting tar archives into the named volumes.
#
# WARNING: This OVERWRITES the current volume contents. Run on a stopped or
# quiesced stack. The operator is responsible for stopping the stack first if
# live services are writing to the volumes.

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
docker compose -f "${REPO_ROOT}/compose.yaml" \
  stop \
  core-app web echo storage knowledge websearch calendar mail tasks 2>/dev/null || true

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
# --------------------------------------------------------------------------
restore_volume() {
  local volume="$1"
  local archive="${BACKUP_DIR}/${volume}.tar.gz"
  if [[ ! -f "${archive}" ]]; then
    log "  (skipping ${volume} — archive not found)"
    return
  fi
  log "Restoring volume ${volume}..."
  # Wipe existing contents before extracting.
  docker run --rm \
    -v "${PROJECT}_${volume}:/data" \
    alpine sh -c "rm -rf /data/* /data/.[!.]*"
  docker run --rm \
    -v "${PROJECT}_${volume}:/data" \
    -v "${BACKUP_DIR}:/backup:ro" \
    alpine \
    tar xzf "/backup/${volume}.tar.gz" -C /data
  log "  → restored ${volume}"
}

for vol in \
  openbao-data \
  qdrant-storage \
  minio-data \
  valkey-data
do
  restore_volume "${vol}"
done

# --------------------------------------------------------------------------
# 4. Restart the stack.
# --------------------------------------------------------------------------
log "Restarting the full stack..."
docker compose -f "${REPO_ROOT}/compose.yaml" up -d

log ""
log "Restore complete. The stack is starting — check Grafana in ~30 s."
log "If OpenBao is sealed after restore, the unseal sidecar will unseal it automatically."
