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
#
# Branch tracking (pre-release box): set both of these in .env to follow a git
# branch's HEAD instead of a release —
#
#   EPICURUS_VERSION=testing          # pull the :testing images CI builds
#   EPICURUS_TRACK_BRANCH=testing     # sync the checkout to origin/testing first
#
# so the compose files / .env.example / new services match the images. Leave
# EPICURUS_TRACK_BRANCH unset for the normal pinned-release flow (no git changes).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${REPO_ROOT}"

log() { echo "[reconcile $(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# Read a single key from .env (values are simple, unquoted tags / branch names).
from_env() {
  [ -f .env ] || return 0
  sed -n "s/^${1}=//p" .env | tail -1
}

TRACK_BRANCH="${EPICURUS_TRACK_BRANCH:-$(from_env EPICURUS_TRACK_BRANCH)}"
VERSION="${EPICURUS_VERSION:-$(from_env EPICURUS_VERSION)}"

# Branch-tracking mode: sync the checkout to the branch HEAD so the compose
# files, .env.example, and any new services match the images about to be pulled.
# Skipped entirely when EPICURUS_TRACK_BRANCH is unset (the pinned-release flow).
# .env / .env.secrets are gitignored, so the hard reset never touches them.
if [ -n "${TRACK_BRANCH}" ]; then
  log "Tracking branch '${TRACK_BRANCH}' — syncing checkout to origin/${TRACK_BRANCH}..."
  git fetch origin "${TRACK_BRANCH}" --quiet
  git checkout "${TRACK_BRANCH}" --quiet 2>/dev/null \
    || git checkout -B "${TRACK_BRANCH}" "origin/${TRACK_BRANCH}" --quiet
  git reset --hard "origin/${TRACK_BRANCH}" --quiet
fi

log "Pulling images (EPICURUS_VERSION=${VERSION:-latest})..."
docker compose pull

log "Restarting updated containers..."
docker compose up -d --remove-orphans

log "Done."
