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

**Verify the setting is active (PowerShell):**

```powershell
Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" |
  Select-Object -Property "Docker Desktop"
```

A non-empty result confirms the registry entry is set.

### What happens on reboot

1. Windows starts → Docker Desktop auto-launches.
2. Docker Engine comes up; all containers with `restart: unless-stopped` start.
3. The `openbao-unseal` sidecar polls `/v1/sys/health` every 30 s and unseals
   OpenBao automatically (ADR-0014).
4. `core-app` waits for `service_healthy` on `openbao` (healthy only when
   unsealed) before starting.
5. Full stack is operational within ~60 s.

## Confirming the stack is healthy

```powershell
# From the repo root — shows all containers and their health status.
docker compose ps
```

All containers should show `running (healthy)` or `running`. Check Grafana at
`http://localhost:3000` → **Alerting → Alert rules** to confirm no alerts are
firing.

## Recovery scenarios

### A service is down

If `docker compose ps` shows a container as `exited` or `restarting`:

```powershell
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
as `model bootstrap: …`. On a slow network the first pull takes a while (the defaults
total ~2.3 GB), and calls that need the model keep 404ing until it lands — that's the
bootstrap still downloading, not a fault.

If the 404s persist:

```powershell
# Did the bootstrap give up? Look for "model bootstrap" lines.
docker compose logs core-app | Select-String "model bootstrap"

# What is actually installed?
docker compose exec ollama ollama list
```

A `giving up on model` warning means the pull exhausted its retries (registry
unreachable) — pull manually from the web UI's **Models** page, or re-run the bootstrap
by restarting the core (`docker compose restart core-app`). `LLM_BOOTSTRAP_MODELS=`
(blank) disables the bootstrap entirely — intended for hosted-only or air-gapped
deployments, where a local 404 instead means the model was simply never pulled.

### OpenBao is sealed {#openbao-sealed}

OpenBao is sealed after the first start until the unseal sidecar runs. It will
also seal if the sidecar crashes or loses its key.

**Check the seal status:**

```powershell
docker compose exec openbao bao status
```

**If `openbao-unseal` is not running:**

```powershell
docker compose restart openbao-unseal
```

The sidecar polls every 30 s and will unseal automatically within 30 seconds
of restarting.

**If the unseal key is lost** (e.g. `.env.secrets` was deleted):
The vault data cannot be recovered without the unseal key. Restore from a
backup — see [Backup and restore](backup-and-restore.md). This is why storing
the unseal key off-box (in a password manager) is essential.

**Manual unseal** (if the sidecar cannot be fixed quickly):

```powershell
$key = Read-Host "Unseal key" -AsSecureString
$plainKey = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($key))
docker compose exec openbao bao operator unseal $plainKey
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

```powershell
docker compose exec -e BAO_TOKEN=$env:OPENBAO_TOKEN openbao bao token lookup
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

```powershell
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
3. If restarting doesn't help, open a PowerShell terminal and run:
   `wsl --shutdown`, then restart Docker Desktop.

### Checking alert history

Active and recently resolved alerts are visible in Grafana at
**Alerting → Alert rules** (Prometheus-managed rules) and
**Alerting → Silences / Contact points** for notification routing.

Historical firing periods appear in the Prometheus expression browser at
`http://localhost:9090` under **Alerts**.
