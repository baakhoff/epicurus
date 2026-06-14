#!/usr/bin/env bash
# On-box pull-reconcile: pull the pinned images and restart any that changed.
#
# Run from the repo root (or schedule via Windows Task Scheduler / WSL cron):
#
#   sh infra/cd/reconcile.sh
#
# The deployed version is controlled by EPICURUS_VERSION in your .env at the
# repo root.  Leave it unset to track :latest; pin it (e.g. "0.2.0") to lock
# to a specific release and upgrade deliberately.  See docs/infrastructure/auto-deploy.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${REPO_ROOT}"

log() { echo "[reconcile $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

log "Pulling images (EPICURUS_VERSION=${EPICURUS_VERSION:-latest})..."
docker compose pull

log "Restarting updated containers..."
docker compose up -d --remove-orphans

log "Done."
