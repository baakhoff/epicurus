#!/usr/bin/env sh
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

# `pipefail` is undefined in POSIX sh (SC3040) — Debian dash rejects it outright ("Illegal
# option"), which given this script's own lesson (#691) would be exactly the wrong kind of
# silent-until-the-real-box failure. `-eu` alone is portable; the one pipeline below
# (`sed | tail`) can't practically fail mid-pipe.
set -eu

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
DOCKER_GID="${DOCKER_GID:-$(from_env DOCKER_GID)}"

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

# Docker-socket opt-in (#622, ADR-0099): persist it across reconciles rather than reverting to
# degraded mode on every run. Only added when the operator has actually set DOCKER_GID (env or
# .env) — the same trigger the overlay itself requires — so a reconcile with nothing set stays
# fail-safe (no socket mount). See services/core-app/compose.docker-socket.yaml and
# docs/infrastructure/auto-deploy.md.
#
# Two constraints shape how this is written, both of which fail *silently* if ignored:
#
#   1. POSIX only — no arrays. The header, Taskfile, and the documented scheduled task all invoke
#      this as `sh infra/cd/reconcile.sh`, and `sh` ignores the shebang. On the deploy box (WSL /
#      Debian) `/bin/sh` is dash, where a bash array is a parse error: the script dies at *load*,
#      after `git reset --hard` has already run in branch-tracking mode — checkout advanced,
#      containers left on the old images. Positional parameters are the portable stand-in.
#   2. Passing any `-f` disables Compose's override auto-discovery. So the default path passes
#      none at all (byte-identical to the plain `docker compose` this replaced), and the opt-in
#      path re-adds a discovered override explicitly — `docker-compose.override.yml` is gitignored
#      precisely so an operator can keep one on the box, and dropping it here would be the same
#      class of silent revert this change exists to fix.
set --
if [ -n "${DOCKER_GID}" ]; then
  log "DOCKER_GID is set — including the Docker-socket opt-in overlay."
  set -- -f compose.yaml
  for override in \
    compose.override.yaml compose.override.yml \
    docker-compose.override.yaml docker-compose.override.yml; do
    if [ -f "${override}" ]; then
      log "Preserving local override ${override}."
      set -- "$@" -f "${override}"
      break
    fi
  done
  set -- "$@" -f services/core-app/compose.docker-socket.yaml
fi

log "Pulling images (EPICURUS_VERSION=${VERSION:-latest})..."
docker compose "$@" pull

log "Restarting updated containers..."
docker compose "$@" up -d --remove-orphans

log "Done."
