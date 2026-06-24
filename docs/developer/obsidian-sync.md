# Keeping the knowledge vault in sync with Obsidian

The knowledge module indexes a plain folder of markdown notes (`/vault` in the
container). To keep that folder in step with an **Obsidian Sync** vault, you point
epicurus at a folder some other process keeps current and let the module watch it —
epicurus never speaks the proprietary Sync protocol itself (it has no public API; see
the [feasibility spike](obsidian-sync-spike.md)). Two setups cover the common cases.

> **Watch mode makes the vault read-only inside epicurus.** When you enable the watcher
> (below), the vault is treated as **externally owned**: the Knowledge editor page goes
> read-only, the file-tree controls disappear, and applying an agent *suggestion* is
> refused (HTTP 409). Obsidian (or Git) is the sole author; epicurus is a pure reader
> that reflects on-disk changes into the index (ADR-0035). Make edits in Obsidian — they
> sync to disk and re-index automatically.

## (a) Same host — bind-mount + watch (recommended)

Run an Obsidian client (with Sync) on the same machine as epicurus — a desktop or a
home server — and bind-mount the synced vault into the `knowledge` container. The
watcher re-indexes incrementally whenever Obsidian Sync lands a change on disk.

1. **Point the shared file space at your vault** and **turn on the watcher.** Knowledge
   reads its projects from `/data/<tenant>/knowledge` (the tenant-scoped `knowledge/`
   subfolder of the shared file space, #KB-refactor; `<tenant>` is `DEFAULT_TENANT_ID`,
   default `local`), so make that subfolder your Obsidian-synced vault — each top-level
   folder in the vault becomes a knowledge base. In your `.env`:

   ```bash
   # The shared file space; your Obsidian vault is its `<tenant>/knowledge/` subfolder, e.g.
   # /home/you/epicurus-files/local/knowledge → /home/you/Obsidian/MyVault (a symlink or the
   # vault placed there directly). "local" is the default DEFAULT_TENANT_ID.
   EPICURUS_FILES_ROOT=/home/you/epicurus-files
   # Watch /data/<tenant>/knowledge and re-index on change.
   VAULT_WATCH=true
   # Optional: how long to coalesce a burst of changes before re-indexing (ms).
   # Obsidian Sync writes many files at once; the default groups them into one pass.
   VAULT_WATCH_DEBOUNCE_MS=1500
   ```

   The container runs as uid **10001** — make sure that user can **read** the vault
   directory (write access is not needed: watch mode never writes the vault).

2. **Start the module** and let the initial index run, then keep Obsidian syncing on
   that host:

   ```bash
   docker compose up -d knowledge
   ```

3. **Verify.** Edit a note in Obsidian (or on disk); within a couple of seconds
   `GET /platform/v1/modules/knowledge/status` shows the counts move, and
   `knowledge_search` returns the new content — no manual re-index.

**What you get:** zero new moving parts beyond the mount; Obsidian remains the source of
truth and owns conflict resolution; works offline; no extra credentials.

**Trade-off:** it needs an Obsidian client running on (or reachable from) the host —
fine for a desktop / home server, awkward for a headless remote VPS. For that, use (b).

## (b) Headless / remote host — Obsidian Git → repo → scheduled pull

On a server with no Obsidian process, use the community **Obsidian Git** plugin on your
*desktop* to commit and push the vault to a Git repo, and have the server pull that repo
into the vault folder on a schedule, then re-index. Start one-way (repo → epicurus); the
editor is read-only in watch mode anyway, so there is no push-back to reconcile.

1. **On your desktop:** install **Obsidian Git**, set it to auto-commit and push your
   vault to a private repo (GitHub, Gitea, a bare repo on the server…).

2. **On the server:** clone that repo to the host path you bind-mount as the vault, and
   bind-mount + watch it exactly as in (a):

   ```bash
   # Clone into the knowledge/ subfolder of the shared file space.
   git clone git@your-host:you/vault.git /srv/epicurus/files/knowledge
   # .env
   EPICURUS_FILES_ROOT=/srv/epicurus/files
   VAULT_WATCH=true
   ```

   Store any deploy key the clone/pull needs in **OpenBao**, not in the repo or the
   compose file (constraint #6 — secrets never touch git).

3. **Pull on a schedule.** A cron entry (or a systemd timer) on the host keeps the clone
   current; the watcher picks up the changed files and re-indexes incrementally:

   ```bash
   # /etc/cron.d/epicurus-vault — pull every 5 minutes
   */5 * * * * epicurus  cd /srv/epicurus/vault && git pull --ff-only
   ```

   `git pull` rewrites the changed note files in place; the watcher debounces the burst
   and runs one incremental pass. (If you prefer event-driven over polling, a repo
   webhook that triggers the pull works too.)

**What you get:** works on a headless box with no Obsidian process; Git gives you version
history and surfaces conflicts; one-way pull sidesteps the two-writer problem entirely.

**Trade-off:** more setup (a plugin, a repo, a deploy key); the pull is periodic, so it
lags by up to the cron interval; two-way (epicurus edits pushed back) is **not** offered —
the watched vault is read-only by design.

## How the watcher behaves

- **Incremental.** Each pass re-embeds only files whose content hash changed; unchanged
  files are skipped on a hash compare, deleted files have their vectors purged.
- **Debounced.** A burst of changes (Obsidian Sync writes many files at once) is
  coalesced into a single pass over `VAULT_WATCH_DEBOUNCE_MS`.
- **Scoped.** Obsidian's `.obsidian/` config directory and `.trash/` are ignored, and
  only `.md` files trigger a pass — config churn and attachments never cause needless
  work.
- **Resilient.** A failed pass (e.g. the core is paused mid-embed) is logged and
  retried on the next change; the watcher never dies on a transient error. A watch pass
  and the startup index never run at once (an indexer run-lock serialises them).

See the [knowledge service page](../services/knowledge.md) for the full configuration
reference and the read-only contract details.
