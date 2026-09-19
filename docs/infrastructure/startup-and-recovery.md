# Startup and recovery

How to keep epicurus running unattended on the operator's Windows box, and what
to do when something goes wrong.

## Start on boot (Windows + Docker Desktop)

The epicurus containers are configured `restart: unless-stopped`, so Docker
Engine automatically starts them after a Docker restart. The only gap is the
host: Docker Desktop must itself start before any container can run.

**Enable Docker Desktop launch-on-login:**

1. Open Docker Desktop.
2. Go to **Settings → General**.
3. Check **Start Docker Desktop when you log in**.
4. Click **Apply & Restart**.

After the next Windows boot, Docker Desktop starts automatically, and the
epicurus stack comes up within ~30 seconds without operator action.

**Verify the setting is active (from a WSL bash shell, via Windows interop):**

```bash
reg.exe query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "Docker Desktop"
```

A result (not "unable to find the specified registry key") confirms the entry is set.

### What happens on reboot

1. Windows starts → Docker Desktop auto-launches.
2. Docker Engine comes up; all containers with `restart: unless-stopped` start.
3. The `openbao-unseal` sidecar polls `/v1/sys/health` every 30 s and unseals
   OpenBao automatically (ADR-0014).
4. `core-app` waits for `service_healthy` on `openbao` (healthy only when
   unsealed) before starting.
5. Full stack is operational within ~60 s.

## Confirming the stack is healthy

```bash
# From the repo root — shows all containers and their health status.
docker compose ps
```

All containers should show `running (healthy)` or `running`. Check Grafana at
`http://localhost:3000` → **Alerting → Alert rules** to confirm no alerts are
firing.

## Recovery scenarios

### A service is down

If `docker compose ps` shows a container as `exited` or `restarting`:

```bash
# Check recent logs for the failing service.
docker compose logs --tail 50 <service-name>

# Restart the service.
docker compose restart <service-name>
```

If the restart loop continues, the container may be crashing on startup. Common
causes: misconfigured environment variable, port conflict, or dependency not yet
healthy. Check the logs for the specific error.

### Chat or embedding fails with "model not found" (404) {#model-not-found}

A fresh install boots an empty Ollama volume — no models. The core bootstraps the
deployment's default chat + embedding models itself at startup (#773, ADR-0118): a
background pull that never blocks readiness, retried with backoff, logged by `core-app`
as `model bootstrap: …`. Since #923 the default (`auto`) seeds an **empty** runtime only —
once the runtime reports any installed model at all, every later start no-ops with an INFO
line saying so, which is what stops a restart silently re-pulling a model the operator
deleted on purpose. An explicit `LLM_BOOTSTRAP_MODELS` list is unchanged: a stated pin,
ensured on every start. On a slow network the first pull takes a while (the defaults
total ~2.3 GB), and calls that need the model keep 404ing until it lands — that's the
bootstrap still downloading, not a fault.

If the 404s persist:

```bash
# Did the bootstrap give up? Look for "model bootstrap" lines.
docker compose logs core-app | grep "model bootstrap"

# What is actually installed?
docker compose exec ollama ollama list
```

A `giving up on model` warning means the pull exhausted its retries (registry
unreachable) — pull it from the web UI's **Models** page. Restarting the core does **not**
retry it: if the other default landed, the runtime is no longer empty and `auto` no-ops
(#923). Name the model in `LLM_BOOTSTRAP_MODELS` if you want a restart to keep trying for
it. `LLM_BOOTSTRAP_MODELS=`
(blank) disables the bootstrap entirely — intended for air-gapped deployments, where a
local 404 instead means the model was simply never pulled.

On a **hosted-only** deployment (`OLLAMA_URL=`, #962) none of this applies: the bootstrap
returns in one log line, and a local model id fails with a **400** naming the mode ("No
local runtime is configured — choose a hosted model") rather than a 404 from a runtime that
is not there. If you *do* see a 404 or a connection error naming Ollama on such a
deployment, the blank `OLLAMA_URL` did not reach the container — check
`docker compose exec core-app printenv OLLAMA_URL` (it must print nothing) and that the
compose interpolation is `${OLLAMA_URL-…}`, not `${OLLAMA_URL:-…}`.

### The Models page shows nothing, or 500s every few seconds {#models-page-empty}

`GET /platform/v1/llm/models` answers `200` with an empty list whenever the local runtime
cannot serve — both when there is none (`OLLAMA_URL` blank) and when one is configured but
unreachable (#962, ADR-0144). Which of the two it is comes from
`GET /platform/v1/llm/local-runtime` → `{"state": "absent"|"unreachable"|"ok"}`:

```bash
docker compose exec core-app python -c \
  "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8080/platform/v1/llm/local-runtime').read().decode())"
```

`absent` is the hosted-only mode (`task hosted-only-up`, or `EPICURUS_HOSTED_ONLY=1` on a
deploy box) and nothing is wrong. `unreachable` means the container is down or the URL is
wrong — check `docker compose ps ollama` and that `OLLAMA_URL` matches it. A 500 from that
endpoint is a bug worth reporting: it was the symptom this contract exists to remove.

### OpenBao is sealed {#openbao-sealed}

OpenBao is sealed after the first start until the unseal sidecar runs. It will
also seal if the sidecar crashes or loses its key.

**Check the seal status:**

```bash
docker compose exec openbao bao status
```

**If `openbao-unseal` is not running:**

```bash
docker compose restart openbao-unseal
```

The sidecar polls every 30 s and will unseal automatically within 30 seconds
of restarting.

**If the unseal key is lost** (e.g. `.env.secrets` was deleted):
The vault data cannot be recovered without the unseal key. Restore from a
backup — see [Backup and restore](backup-and-restore.md). This is why storing
the unseal key off-box (in a password manager) is essential.

**Manual unseal** (if the sidecar cannot be fixed quickly):

```bash
read -s -p "Unseal key: " key; echo
docker compose exec openbao bao operator unseal "$key"
```

### Every secret read fails with "permission denied" {#openbao-token-expired}

Symptom: hosted-model turns fail with
`failed to read secret tenants/local/llm/<provider>: permission denied`, the Models page
shows every provider as "key not set", and Google mail/calendar stop authenticating — all at
once. After a restart it becomes `OpenBao client is not authenticated` and core-app refuses
to start. The vault is unsealed and the policy is fine: **the token was rejected**, not the
path.

On deployments bootstrapped before the periodic-token fix this happens exactly 32 days after
bootstrap, because the app token was a plain service token on the 768h default lease. Newer
bootstraps mint a periodic token that core-app renews daily, so this should only appear if
renewal has been failing — check for `openbao token renewal failed` in the core's logs, which
warns weeks before the lease actually runs out.

**Confirm** (a live token prints a TTL; an expired one 403s):

```bash
docker compose exec -e BAO_TOKEN=$OPENBAO_TOKEN openbao bao token lookup
```

**Recover** — mint a periodic replacement with the root token from `.env.secrets`, put it in
`.env` as `OPENBAO_TOKEN`, then **recreate** (not restart) the services that hold it. The full
procedure, including what to verify afterwards, is in
[Secrets → Token lifetime & renewal](secrets.md#token-lifetime--renewal). No secrets are lost;
only the credential used to read them.

### Disk space is low

The DiskSpaceHigh alert fires when the WSL2 VM filesystem is above 85% full.
This is the filesystem where Docker stores named volumes.

**Check current usage:**

```bash
# From inside a container that has the WSL2 root mounted:
docker run --rm -v /:/rootfs:ro alpine df -h /rootfs
```

**Free space:**

1. Remove unused Docker images: `docker image prune -a`
2. Remove stopped containers: `docker container prune`
3. Remove unused volumes (caution — verify before running):
   `docker volume prune`
4. Expand the WSL2 VHDX if the host disk has room:
   see [WSL2 disk resize guide](https://learn.microsoft.com/en-us/windows/wsl/disk-space).

### Stack not coming up after a Windows update

Docker Desktop occasionally needs to be restarted after major Windows updates
(especially WSL2 kernel updates).

1. Check Docker Desktop's status in the system tray.
2. If it shows an error, right-click → **Restart**.
3. If restarting doesn't help, from a WSL bash shell run `wsl.exe --shutdown` (Windows
   interop puts `wsl.exe` on the WSL `PATH`), then restart Docker Desktop.

### Checking alert history

Active and recently resolved alerts are visible in Grafana at
**Alerting → Alert rules** (Prometheus-managed rules) and
**Alerting → Silences / Contact points** for notification routing.

Historical firing periods appear in the Prometheus expression browser at
`http://localhost:9090` under **Alerts**.

### The Files view or knowledge search looks empty after a boot {#mass-deindex-fuse}

A cold boot can bring the stack up with a **stale bind mount**: the container sees an empty
`/data` while the real file space is intact on the host. Everything downstream then reads
"there are no files" as truth. That is what happened on 2026-08-30 — the core's file scan
reconciled `core_files` to zero rows and a knowledge re-index emptied the vault's Qdrant
collection, with no error logged anywhere.

Since #848 the derived indexes **refuse** that reconciliation instead of performing it (the
*mass de-index fuse*). What you will see:

- `epicurus-core-app` logs `mass de-index fuse tripped; index purge refused` at `ERROR`, with
  the row counts, and `GET /platform/v1/files/scan-status` lists the refusing namespace.
- `epicurus-knowledge` logs its equivalent (`mass de-index fuse tripped; index pass refused`)
  per source; the Modules page's **knowledge** status
  panel shows `index fuse tripped: true` with the detail, and a `POST /reindex` answers
  `{"status": "refused", …}` rather than starting.
- The metrics `epicurus_core_file_scan_fuse_tripped` and
  `epicurus_knowledge_index_fuse_tripped` sit at `1`.

The indexes are **stale, not lost** — the rows and vectors were kept. Recover the mount, not
the index:

1. Confirm the file space really is populated on the host (`ls` the directory
   `EPICURUS_FILES_ROOT` points at, then `docker compose exec core-app ls /data/<tenant>`;
   the container view is the one that lies).
2. If the container's view is empty, restart the WSL2 backend and recreate the container:
   `wsl.exe --shutdown`, restart Docker Desktop, then
   `docker compose up -d --force-recreate core-app`.
3. With `/data` visible again, re-run the scan and the index:
   `curl -X POST 'http://localhost:8082/platform/v1/files/rescan'` (core-app's published host
   port — `CORE_PORT`, see [ports](../reference/ports.md); `8080` is the echo module) and
   knowledge's **Re-index** action (or `POST /reindex`). Both re-arm their fuse on the first
   clean pass.

Only if the file space genuinely lost its contents should the indexes follow it down: add
`?force=true` to the rescan and the re-index. `force` deletes the derived state the fuse is
protecting — confirm step 1 before using it.
