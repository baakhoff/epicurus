# Auto-deploy (CD)

How a released tag becomes a running stack update on the operator's box —
and how to roll it back if something goes wrong.

## How it works

```
git tag vX.Y.Z  →  release.yml builds images  →  push to GHCR
                                                          ↓
                                              box reconcile pulls & restarts
```

1. **Tag a release.** Cut a semver tag from `main` after CI is green:
   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```
2. **`release.yml` builds + pushes** every `services/*/Dockerfile` to GHCR as
   `ghcr.io/baakhoff/epicurus-<svc>:<semver>` and `ghcr.io/baakhoff/epicurus-<svc>:latest`.
   The GitHub Release is then created (notes generated from merged PRs).
3. **The box reconciles** — either via a scheduled script or an always-on
   Watchtower container — pulling the new images and restarting updated containers.

The box **never accepts inbound connections from GitHub**.  It pulls; nothing dials in.

## EPICURUS_VERSION

All service compose fragments resolve the image tag from `${EPICURUS_VERSION:-latest}`.

| `.env` value | Behaviour |
| --- | --- |
| Unset / `latest` | Always pulls the most recent **release** image on the next reconcile. |
| `0.2.0` (pinned) | Always runs that exact image; upgrade is a deliberate `.env` edit + reconcile. |
| `testing` | Tracks the **`testing` branch** HEAD — see below. Runs ahead of releases. |

For a personal single-operator box, tracking `:latest` is the most convenient
setup: tag → images on GHCR → next scheduled reconcile deploys it automatically.

Set a pinned value if you want to control *when* upgrades happen, or to run a
specific version on staging while production lags behind.

### Tracking the `testing` branch (pre-release box)

To run a box straight off a branch instead of cut releases — handy for a personal
always-on box you want to dogfood before tagging a release — push to the `testing`
branch. The [`Testing images`](../../.github/workflows/testing.yml) workflow rebuilds
every service image on each push and publishes them to GHCR under the moving
`:testing` tag (no version tag, no GitHub Release).

On the box, set **both** of these in `.env`:

```env
EPICURUS_VERSION=testing        # pull the :testing images
EPICURUS_TRACK_BRANCH=testing   # also sync the checkout to origin/testing
```

`EPICURUS_TRACK_BRANCH` makes `reconcile.sh` `git reset --hard origin/testing` before
pulling, so the **compose files, `.env.example`, and any new services** move with the
branch — not just the image tags. (`.env`/`.env.secrets` are gitignored, so the reset
never touches your secrets.) Leave it unset for the normal release flow.

Then the same scheduled reconcile (Option A) or Watchtower (Option B) below applies
unchanged — the box just follows `testing` instead of `:latest`. Caveat: `testing`
is intentionally *unprotected and unverified* — whatever you push runs on the box, so
keep `main` for vetted code and treat the testing box as disposable.

## Option A — Scheduled reconcile script (recommended for Windows)

`infra/cd/reconcile.sh` runs `docker compose pull && docker compose up -d --remove-orphans`
from the repo root (or `task reconcile`, the same script).  Schedule it with **Windows Task
Scheduler** so it runs while you sleep — or run it by hand for an immediate deploy.

**Docker-socket opt-in survives reconcile only if `DOCKER_GID` is in `.env` (#655).** A manual
`task docker-socket-up` mounts the socket for that one `up`, but the next scheduled reconcile
recreates `core-app` from plain `compose.yaml` with no overlay, silently reverting to degraded
mode (fails safe, but the opt-in doesn't stick). Set `DOCKER_GID` in `.env` alongside
`EPICURUS_VERSION` and every reconcile includes
`services/core-app/compose.docker-socket.yaml` automatically — see
[Docker-socket access](index.md#docker-socket-access-opt-in-622) for the value to use.

### One-time GHCR login

Images on a private repo require authentication.  Run this once (Docker caches the
credential):

```powershell
docker login ghcr.io -u <your-github-username> --password-stdin
# paste a GitHub PAT with read:packages scope, then Ctrl-Z Enter
```

### Register the scheduled task (PowerShell — run as administrator)

```powershell
$action = New-ScheduledTaskAction `
    -Execute "wsl.exe" `
    -Argument "-e bash -c 'cd /mnt/c/Users/baakh/Documents/Projects/epicurus && sh infra/cd/reconcile.sh >> /tmp/epicurus-reconcile.log 2>&1'"

$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -Once -At (Get-Date)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName "EpicurusReconcile" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force
```

This runs the reconcile every **30 minutes**.  Adjust `-RepetitionInterval` to taste.

> **Path note:** replace the WSL path with the actual location of your repo on the WSL
> filesystem if you cloned inside WSL rather than on the Windows drive.

### Verify the task

```powershell
Get-ScheduledTask -TaskName "EpicurusReconcile" | Select-Object State, LastRunTime
```

To trigger a manual run:

```powershell
Start-ScheduledTask -TaskName "EpicurusReconcile"
```

### WSL cron alternative

If you prefer a cron job inside WSL:

```bash
# inside WSL
crontab -e
# add:
*/30 * * * *  cd /mnt/c/Users/baakh/Documents/Projects/epicurus && sh infra/cd/reconcile.sh >> /tmp/epicurus-reconcile.log 2>&1
```

WSL cron only runs while the WSL session is active.  The Task Scheduler approach
keeps reconciling even with no open WSL terminal.

## Option B — Watchtower (automatic :latest tracking)

[Watchtower](https://containrrr.dev/watchtower/) is a container that polls your
running images for digest changes and restarts them in place.  It requires no OS
scheduling and works on any platform.

**When to prefer Watchtower over the reconcile script:**
- You want zero-touch upgrades: tag → GHCR → box updates on its own.
- You don't mind every running container being watched (see the note below).

**When to prefer the reconcile script:**
- You set `EPICURUS_VERSION` to a pinned version — Watchtower's `:latest` poll
  won't move a pinned tag.
- This box runs containers unrelated to epicurus that you don't want auto-updated.

### Setup

Add the GHCR credentials to your root `.env`:

```env
GHCR_USERNAME=<your-github-username>
GHCR_TOKEN=<ghp_...>   # PAT with read:packages scope
```

Start Watchtower alongside the stack:

```bash
docker compose -f compose.yaml -f infra/cd/watchtower.yaml up -d watchtower
```

Watchtower polls GHCR every 5 minutes (`WATCHTOWER_POLL_INTERVAL=300`).  When it
finds a new image digest it pulls the image, stops the old container, and starts a
new one in-place — no compose restart needed.

## Rollback

A rollback is a `.env` edit + reconcile — the previous image is already on GHCR.

1. Set `EPICURUS_VERSION` in `.env` to the last known-good semver (without `v`):
   ```env
   EPICURUS_VERSION=0.1.0
   ```
2. Pull and restart:
   ```bash
   docker compose pull
   docker compose up -d
   ```

Secrets and data are untouched — they live in named Docker volumes, not the image.
The core runs the same migrations on startup; rolling back to an older image that
ran against the same schema is safe unless the newer version ran destructive
migrations (which would be called out in the release notes).

To confirm the running version:

```bash
docker inspect ghcr.io/baakhoff/epicurus-core-app:$(grep EPICURUS_VERSION .env | cut -d= -f2) \
  --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
```

## Verifying a deploy

After the reconcile runs (or Watchtower restarts containers):

```bash
docker compose ps          # all services running / healthy
docker compose logs --tail 20 core-app   # no startup errors
```

Check Grafana at `http://localhost:3000` → **Alerting → Alert rules** to confirm
no alerts are firing.

A fresh deploy's **Modules** page may show a status card saying Docker isn't reachable — that
is the **expected default** (#622, ADR-0099), not a broken deploy: module removal still works
immediately, only container teardown (and an Ollama KV-cache restart) defer to the next
restart. See [Docker-socket access](index.md#docker-socket-access-opt-in-622) to opt into
immediate teardown instead.
