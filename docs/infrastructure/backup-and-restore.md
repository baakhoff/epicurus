# Backup and restore

Minimal backup posture for epicurus: snapshot the stateful volumes and keep the
OpenBao unseal key off-box. Full automated backup with Restic is a Phase 6 goal;
this runbook covers what is needed to not lose data unattended.

## What is backed up

| Data | Storage | Backup method |
| --- | --- | --- |
| **Postgres** (conversations, memory, llm prefs, calendar/tasks local data) | `epicurus_postgres-data` volume | `pg_dumpall` → compressed SQL |
| **OpenBao secrets** | `epicurus_openbao-data` volume | Volume snapshot |
| **Qdrant vectors** | `epicurus_qdrant-data` volume | Volume snapshot |
| **Knowledge vault** | `epicurus_knowledge-vault-data` volume | Volume snapshot |
| **Storage files** | `epicurus_storage-root-data` volume | Volume snapshot |
| **MinIO objects** | `epicurus_minio-data` volume | Volume snapshot |
| **Valkey cache** | `epicurus_valkey-data` volume | Volume snapshot (optional) |

Observability volumes (Prometheus, Loki, Grafana, Tempo) are not business-critical
and are excluded by default — they rebuild from live data within minutes.

## Unseal key — store this off-box first

The OpenBao unseal key (`OPENBAO_UNSEAL_KEY` in `infra/compose/.env.secrets`) is
the single key that unlocks all stored secrets. Without it, the volume backup
is unreadable.

**Store the unseal key in a password manager before running the stack unattended.
This is a prerequisite, not optional.**

```powershell
# Print the unseal key (copy to your password manager immediately).
Get-Content infra\compose\.env.secrets | Select-String "OPENBAO_UNSEAL_KEY"
```

The file is gitignored. Never commit it. If this machine is lost, the key in
your password manager is the recovery path.

## Running a backup

```bash
# From the repo root:
bash infra/backups/backup.sh [DEST_DIR]
```

`DEST_DIR` defaults to `./backups/<timestamp>/`. The script:

1. Runs `pg_dumpall` inside the Postgres container (consistent logical dump).
2. Tars each named volume using a temporary Alpine container.
3. Writes a `manifest.json` with timestamps and file list.

The stack stays running during the backup. Postgres is safely dumped live.
The other volumes are snapshotted live — acceptable for single-operator use.

**Example:**

```bash
bash infra/backups/backup.sh /mnt/d/epicurus-backups/
# → /mnt/d/epicurus-backups/20260614T120000Z/postgres.sql.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/openbao-data.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/qdrant-data.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/knowledge-vault-data.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/storage-root-data.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/minio-data.tar.gz
# → ...
```

**Store backups off the machine.** Copy the backup directory to an external drive,
NAS, or cloud storage. A backup on the same disk offers no protection against
disk failure.

## Scheduling regular backups

### Windows Task Scheduler (recommended)

1. Open **Task Scheduler** → **Create Task**.
2. **General**: Name = "epicurus backup", run whether user is logged on or not.
3. **Triggers**: Daily at a time when the machine is likely on (e.g. 03:00).
4. **Actions**: Start a program:
   - Program: `C:\Windows\System32\wsl.exe`
   - Arguments: `-e bash /path/to/epicurus/infra/backups/backup.sh D:/epicurus-backups`
5. **Conditions**: Uncheck "Start only if computer is on AC power" if on a laptop.

### WSL2 cron (alternative)

```bash
# Inside WSL2:
crontab -e
# Add (runs daily at 02:30, adjust path as needed):
30 2 * * * cd /mnt/c/Users/you/Documents/Projects/epicurus && bash infra/backups/backup.sh /mnt/d/epicurus-backups >> /tmp/epicurus-backup.log 2>&1
```

## Verified restore procedure {#verified-restore}

Test this before you need it. Run on a test machine or against a disposable stack.

```bash
# Stop app services first (infra stays up for Postgres restore):
docker compose stop core-app web echo storage knowledge websearch calendar mail tasks

# Restore from a backup directory:
bash infra/backups/restore.sh /mnt/d/epicurus-backups/20260614T120000Z/

# The script:
# 1. Restores Postgres via psql from the .sql.gz dump.
# 2. Wipes and restores each named volume from its .tar.gz archive.
# 3. Restarts the full stack.
```

After restore, verify:

- `docker compose ps` — all containers healthy.
- Grafana at `http://localhost:3000` — no alerts firing.
- `docker compose exec openbao bao status` — vault is active (unsealed).
- Chat with epicurus — confirm memory and settings are intact.

## Disk space {#disk-space}

The **DiskSpaceHigh** Prometheus alert fires when the WSL2 VM filesystem exceeds
85% full. This is the filesystem where Docker stores named volumes on Windows.

**Check current usage:**

```bash
# Inside WSL2 / from a container:
df -h /
```

**Reclaim space:**

```bash
# Remove dangling images:
docker image prune -a

# Remove stopped containers:
docker container prune

# Compact the WSL2 VHDX (run from PowerShell as Administrator):
# wsl --shutdown
# Optimize-VHD -Path "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx" -Mode Full
```

**Expand the VHDX** if the host disk has room: see the
[WSL2 disk space guide](https://learn.microsoft.com/en-us/windows/wsl/disk-space).
