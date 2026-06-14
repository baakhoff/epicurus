#!/usr/bin/env bash
# Snapshot epicurus named volumes to a timestamped tar archive.
#
# Usage:
#   ./infra/backups/backup.sh [DEST_DIR]
#
# DEST_DIR defaults to ./backups/<timestamp>/ in the repo root.
# The script does NOT stop the stack; Postgres is flushed via pg_dumpall
# for consistency. MinIO, Qdrant, and the other volumes are snapshotted
# live (acceptable for a personal, single-tenant deployment).
#
# For Postgres, pg_dumpall produces a logical dump that is both portable
# and smaller than a raw volume snapshot. The other volumes are archived
# from a temporary Alpine container that mounts each named volume.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${1:-"${REPO_ROOT}/backups/${TIMESTAMP}"}"

# Compose project name (matches the root compose.yaml `name:` field).
PROJECT="${COMPOSE_PROJECT_NAME:-epicurus}"

mkdir -p "${DEST}"

log() { echo "[backup] $*"; }

# --------------------------------------------------------------------------
# 1. Postgres — logical dump via pg_dumpall (consistent across the stack).
# --------------------------------------------------------------------------
log "Dumping Postgres..."
docker compose -f "${REPO_ROOT}/infra/compose/docker-compose.yml" \
  exec -T postgres \
  pg_dumpall -U "${POSTGRES_USER:-epicurus}" \
  | gzip > "${DEST}/postgres.sql.gz"
log "  → ${DEST}/postgres.sql.gz"

# --------------------------------------------------------------------------
# 2. Named volumes — tar snapshot using a temporary Alpine container.
#    User data: openbao-data (secrets), qdrant-data (vectors), minio-data
#    (objects), knowledge-vault-data (vault), storage-root-data (files),
#    valkey-data (cache). calendar/tasks local data lives in Postgres (dumped
#    above). Observability volumes are excluded — they rebuild from live data.
# --------------------------------------------------------------------------
snapshot_volume() {
  local volume="$1"
  local archive="${DEST}/${volume}.tar.gz"
  log "Snapshotting volume ${volume}..."
  docker run --rm \
    -v "${PROJECT}_${volume}:/data:ro" \
    -v "${DEST}:/backup" \
    alpine \
    tar czf "/backup/${volume}.tar.gz" -C /data .
  log "  → ${archive}"
}

for vol in \
  openbao-data \
  qdrant-data \
  minio-data \
  knowledge-vault-data \
  storage-root-data \
  valkey-data
do
  # Skip volumes that do not exist for this installation.
  if docker volume inspect "${PROJECT}_${vol}" > /dev/null 2>&1; then
    snapshot_volume "${vol}"
  else
    log "  (skipping ${vol} — not found)"
  fi
done

# --------------------------------------------------------------------------
# 3. Write a manifest.
# --------------------------------------------------------------------------
log "Writing manifest..."
cat > "${DEST}/manifest.json" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "project": "${PROJECT}",
  "host": "$(hostname)",
  "files": $(ls "${DEST}" | grep -v manifest.json | python3 -c "import sys, json; print(json.dumps([l.rstrip() for l in sys.stdin]))")
}
EOF

log "Backup complete: ${DEST}"
