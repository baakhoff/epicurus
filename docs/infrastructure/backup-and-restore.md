# Backup and restore

Minimal backup posture for epicurus: snapshot the stateful data and keep the
OpenBao unseal key off-box. This runbook covers what is needed to not lose data
unattended; a fuller automated posture (Restic, retention, off-site rotation) is a
later milestone, and this page does not pretend otherwise.

**This page is the Docker path.** `infra/backups/backup.sh` and `restore.sh`
address Docker named volumes and the host directory `EPICURUS_FILES_ROOT` may point
at. There is **no Kubernetes backup yet**: a `CronJob` in the chart plus a matching
restore runbook is the open half of #895. On a cluster, back up the
`epicurus-openbao` Secret (it holds the unseal key) and snapshot the PVCs with your
cluster's own tooling — see [Kubernetes](kubernetes.md#known-limitations).

## This is not data portability

Two different jobs, deliberately kept apart:

| | **Backup & restore** (this page) | **[Export & import](../user/export-import.md)** (#867) |
| --- | --- | --- |
| Unit | this deployment's **volumes** | one **tenant's** logical data |
| Restores | the same machine, byte for byte | *another* installation, merged in |
| Carries secrets | yes (the OpenBao volume) | **no** — names only, re-entered by hand |
| Carries derived state | yes (Qdrant vectors, indexes) | no — rebuilt on import |
| Driven from | a shell, on the host | the web UI, Settings → Export & import |
| Answers | "the disk died" | "I am moving to a new box" |

Use both. A tenant export is not a backup — it deliberately omits secrets and every derived
index, so it cannot put a dead machine back. And a volume backup is not portable — it
assumes the same volume layout, the same secrets, and the same embedding model.

## What is backed up

| Data | Storage | Backup method |
| --- | --- | --- |
| **Postgres** (conversations, memory, llm prefs, the event log, every module's tables) | `epicurus_postgres-data` volume | `pg_dumpall` → compressed SQL |
| **OpenBao secrets** | `epicurus_openbao-data` volume | Volume snapshot |
| **Qdrant vectors** | `epicurus_qdrant-data` volume | Volume snapshot |
| **MinIO objects** | `epicurus_minio-data` volume | Volume snapshot |
| **The shared file space** (knowledge documents, the notes mirror, everything under the core's `/data`) | `epicurus_epicurus-files` volume **or** the host directory `EPICURUS_FILES_ROOT` points at | Snapshot of whichever it is → `epicurus-files.tar.gz` |

### What is deliberately *not* backed up

Because "not in the list" and "forgotten" looked identical in this script before
(#895), each exclusion is a decision with a reason:

| Not archived | Why |
| --- | --- |
| `postgres-data` (raw volume) | Dumped logically instead. A raw snapshot of a live cluster is the worse of the two copies, and shipping both invites a restore that mixes them. |
| `valkey-data` | A cache (`--appendonly no`), and nothing in the codebase reads it. Restoring a stale cache is worse than starting empty. |
| `nats-data` | JetStream's in-flight messages and consumer cursors. Reinstating an old cursor is not a recovery, it is a second delivery — and the durable event *log* is in Postgres. |
| Observability volumes | Prometheus, Loki, Grafana, Tempo rebuild from live data within minutes. |

`tests/test_backup_volumes.py` keeps both scripts honest: every name they address
must be a volume the compose files declare (a missing one is *skipped*, not an
error, so a stale name shrinks the backup silently), backup and restore must agree,
and any new data-plane volume must be either archived or named in the exclusion
list above — so the next one is a decision, not an omission.

### The file space lives in one of two places

`EPICURUS_FILES_ROOT` decides, using compose's own rule for the short volume
syntax:

- **unset (the default)** — the `epicurus-files` named volume; the scripts snapshot
  it like any other volume.
- **a path** (a value containing `/`, e.g. `/srv/epicurus-files`) — a host bind
  mount. There is no named volume to snapshot, so the scripts archive the directory
  instead, through a container so the tree's real uids survive.

Either way the archive is called `epicurus-files.tar.gz`, and `manifest.json`
records which shape it came from (`files_source`). A restore reads **this** host's
`EPICURUS_FILES_ROOT`, so moving a deployment from a named volume to a bind mount
(or back) restores cleanly.

> Until #895 the loop carried two volumes that had not existed since the file space
> moved to `epicurus-files` — and did not carry `epicurus-files` itself. A backup
> taken before this fix contains no user files. Take a fresh one.

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
3. Archives the shared file space — the named volume, or the host directory
   `EPICURUS_FILES_ROOT` points at — and says in the log which it did.
4. Writes a `manifest.json` with the timestamp, the file list, and `files_source`.

The stack stays running during the backup. Postgres is safely dumped live.
The other volumes are snapshotted live — acceptable for single-operator use.

**Example:**

```bash
bash infra/backups/backup.sh /mnt/d/epicurus-backups/
# → /mnt/d/epicurus-backups/20260614T120000Z/postgres.sql.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/openbao-data.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/qdrant-data.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/minio-data.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/epicurus-files.tar.gz
# → /mnt/d/epicurus-backups/20260614T120000Z/manifest.json
```

Read the log. A line saying the file space was **NOT** backed up (a host path that
does not exist on this machine, or a missing volume) is the one failure this script
deliberately does not exit non-zero for — it still saves everything else — and it
is the line that matters most.

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
# Restore from a backup directory:
bash infra/backups/restore.sh /mnt/d/epicurus-backups/20260614T120000Z/

# The script:
# 1. Stops every application service (the list is derived from compose.yaml's
#    `include:`, so it can never be a module short) — infra stays up for Postgres.
# 2. Restores Postgres via psql from the .sql.gz dump.
# 3. Wipes and restores each named volume from its .tar.gz archive.
# 4. Restores the file space into whatever EPICURUS_FILES_ROOT points at HERE —
#    a named volume or a host directory; it need not match the source machine.
# 5. Restarts the full stack.
```

After restore, verify:

- `docker compose ps` — all containers healthy.
- Grafana at `http://localhost:3000` — no alerts firing.
- `docker compose exec openbao bao status` — vault is active (unsealed).
- `docker compose exec core-app ls /data/local` — the file space came back (use your
  own `DEFAULT_TENANT_ID` in place of `local`). An empty `/data` here after a restore
  that reported success means the backup was taken before #895 fixed the file space,
  or `EPICURUS_FILES_ROOT` differs between the two machines.
- Open **Files** in the web UI — the tree matches what you had.
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
