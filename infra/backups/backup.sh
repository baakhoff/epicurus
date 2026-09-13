#!/usr/bin/env bash
# Snapshot epicurus's stateful data to a timestamped tar archive.
#
# Usage:
#   ./infra/backups/backup.sh [DEST_DIR]
#
# DEST_DIR defaults to ./backups/<timestamp>/ in the repo root.
# The script does NOT stop the stack; Postgres is flushed via pg_dumpall
# for consistency. MinIO, Qdrant, and the other volumes are snapshotted
# live (acceptable for a personal, single-tenant deployment).
#
# For Postgres, pg_dumpall produces a logical dump that is both portable and
# smaller than a raw volume snapshot. Everything else is archived from a
# temporary Alpine container that mounts the volume (or the host directory), so
# the uids inside the tree survive whoever runs this script.
#
# THIS IS THE DOCKER PATH. The Kubernetes equivalent — a CronJob in the chart and
# the restore runbook to match — is still open as #895; see
# docs/infrastructure/backup-and-restore.md.

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
#
#    Only the volumes that hold data no restore could rebuild:
#      openbao-data  the secrets (without it the rest is unusable)
#      qdrant-data   the vectors
#      minio-data    the objects
#
#    What is deliberately NOT archived, and why — because "it is not in the list"
#    has been indistinguishable from "it was forgotten" in this file before:
#      postgres-data  dumped logically above; a raw snapshot of a live cluster is
#                     the worse of the two copies, and shipping both invites a
#                     restore that mixes them.
#      valkey-data    a cache (`--appendonly no`). Restoring a stale cache is
#                     worse than starting with an empty one.
#      nats-data      JetStream's in-flight messages and consumer cursors.
#                     Reinstating an old cursor is not recovery, it is a second
#                     delivery; the durable event *log* is in Postgres.
#      observability  Prometheus / Loki / Grafana / Tempo rebuild from live data.
#
#    Until #895 this loop also named `knowledge-vault-data` and
#    `storage-root-data`, which had not existed since the file space moved to
#    `epicurus-files` — so every backup exited 0 having silently skipped two
#    lines. `epicurus-files` itself, the operator's whole file tree, was not in
#    the loop at all; it is handled in step 3. tests/test_backup_volumes.py now
#    holds both scripts to the volumes the compose files actually declare.
# --------------------------------------------------------------------------
VOLUMES=(openbao-data qdrant-data minio-data)

snapshot_volume() {
  local volume="$1"
  local name="${2:-$1}"
  local archive="${DEST}/${name}.tar.gz"
  log "Snapshotting volume ${volume}..."
  docker run --rm \
    -v "${PROJECT}_${volume}:/data:ro" \
    -v "${DEST}:/backup" \
    alpine \
    tar czf "/backup/${name}.tar.gz" -C /data .
  log "  → ${archive}"
}

snapshot_host_dir() {
  local dir="$1"
  local name="$2"
  local archive="${DEST}/${name}.tar.gz"
  log "Archiving host directory ${dir}..."
  # Through a container, like the volumes: it runs as root, so the tree's real
  # uids (the core owns it as 10001, ADR-0069) survive into the archive even when
  # the operator running this script cannot read every file.
  docker run --rm \
    -v "${dir}:/data:ro" \
    -v "${DEST}:/backup" \
    alpine \
    tar czf "/backup/${name}.tar.gz" -C /data .
  log "  → ${archive}"
}

for vol in "${VOLUMES[@]}"; do
  # Skip volumes that do not exist for this installation.
  if docker volume inspect "${PROJECT}_${vol}" > /dev/null 2>&1; then
    snapshot_volume "${vol}"
  else
    log "  (skipping ${vol} — not found)"
  fi
done

# --------------------------------------------------------------------------
# 3. The shared file space (ADR-0063) — the operator's whole file tree.
#
#    The core mounts it at /data and it is the source of truth behind knowledge,
#    notes and storage: lose it and Postgres still holds the metadata for
#    documents whose bytes are gone. It was in no backup at all until #895.
#
#    EPICURUS_FILES_ROOT decides what there is to archive, using compose's own
#    rule for the short volume syntax: a value containing a `/` is a HOST PATH
#    (the owner's box uses /srv/epicurus-files) and there is no named volume to
#    snapshot; anything else — including the unset default — is a named volume.
#    Either way the archive is written as epicurus-files.tar.gz, and the manifest
#    records which it was, so a restore does not have to guess.
# --------------------------------------------------------------------------
FILES_ROOT="${EPICURUS_FILES_ROOT:-}"
if [[ -z "${FILES_ROOT}" && -f "${REPO_ROOT}/.env" ]]; then
  # Same file compose itself reads (the project directory is the repo root).
  FILES_ROOT="$(sed -n 's/^[[:space:]]*EPICURUS_FILES_ROOT=//p' "${REPO_ROOT}/.env" | tail -1)"
  FILES_ROOT="${FILES_ROOT%$'\r'}"
  FILES_ROOT="${FILES_ROOT%\"}"; FILES_ROOT="${FILES_ROOT#\"}"
  FILES_ROOT="${FILES_ROOT%\'}"; FILES_ROOT="${FILES_ROOT#\'}"
fi
FILES_SOURCE=""
if [[ "${FILES_ROOT}" == */* ]]; then
  if [[ -d "${FILES_ROOT}" ]]; then
    snapshot_host_dir "${FILES_ROOT}" epicurus-files
    FILES_SOURCE="host directory ${FILES_ROOT}"
  else
    log "  WARNING: EPICURUS_FILES_ROOT=${FILES_ROOT} is not a directory on this host —"
    log "           the file space was NOT backed up. Run this on the box that holds it."
    FILES_SOURCE="MISSING (${FILES_ROOT} not found)"
  fi
else
  files_volume="${FILES_ROOT:-epicurus-files}"
  if docker volume inspect "${PROJECT}_${files_volume}" > /dev/null 2>&1; then
    snapshot_volume "${files_volume}" epicurus-files
    FILES_SOURCE="named volume ${PROJECT}_${files_volume}"
  else
    log "  WARNING: neither EPICURUS_FILES_ROOT nor the ${PROJECT}_${files_volume} volume"
    log "           exists — the file space was NOT backed up."
    FILES_SOURCE="MISSING (no ${PROJECT}_${files_volume} volume)"
  fi
fi
log "File space: ${FILES_SOURCE}"

# --------------------------------------------------------------------------
# 4. Write a manifest.
# --------------------------------------------------------------------------
log "Writing manifest..."
cat > "${DEST}/manifest.json" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "project": "${PROJECT}",
  "host": "$(hostname)",
  "files_source": "${FILES_SOURCE}",
  "files": $(cd "${DEST}" && python3 -c "import json, os; print(json.dumps(sorted(f for f in os.listdir('.') if f != 'manifest.json')))")
}
EOF

log "Backup complete: ${DEST}"
