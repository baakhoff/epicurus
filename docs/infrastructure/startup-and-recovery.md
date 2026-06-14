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
