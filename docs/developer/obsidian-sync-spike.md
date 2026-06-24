# Spike: syncing the knowledge vault with Obsidian Sync (#219)

**Status:** feasibility spike — recommendation below; implementation tracked as a
follow-up issue. **Date:** 2026-06-18.

## The ask

The knowledge vault lives in the `knowledge` container. An operator who keeps their
notes in Obsidian and pays for **Obsidian Sync** wants the knowledge base to stay in
step with that vault, ideally both ways.

## Reality check — no native Obsidian Sync integration

**Obsidian Sync is a proprietary, end-to-end-encrypted service with no public API.**
There is no supported way to read or write a synced vault except through a running
Obsidian client. Reverse-engineering the sync protocol or its endpoints is out
(constraint: *user-controlled data only; no scraping of proprietary endpoints*, and it
would be brittle and a ToS problem). So "epicurus speaks Obsidian Sync" is **not
feasible**. The realistic options all sync a **plain folder of markdown** that some
other process keeps in step with Obsidian — epicurus just indexes that folder.

## Options evaluated

### (a) Bind-mount a folder the user already syncs — **recommended**

The operator runs Obsidian (with Sync) on the same host as epicurus, or onto a folder
that host can see, and bind-mounts that vault directory into the `knowledge` container.

epicurus **already supports the mount**: the shared file space (`EPICURUS_FILES_ROOT`, the
`knowledge/` subfolder mounted at `/data/knowledge` — read-write, container uid 10001; see
[knowledge](../services/knowledge.md)) holds the vault.
So the only real gap is **change detection**: today the index refreshes on startup or on
an explicit `knowledge_reindex`; edits that Obsidian Sync lands in the folder aren't
picked up until then.

- **Pros:** zero new moving parts; reuses the existing mount and the incremental
  indexer; Obsidian remains the source of truth and owns conflict resolution; works
  offline; no extra credentials.
- **Cons:** requires Obsidian (with Sync) running on/reachable from the host — fine for
  a desktop/home-server box, awkward for a headless remote VPS; two writers (the
  Obsidian client and the knowledge **editor page**, #130) touch the same files, so
  write-back needs care (see *Conflicts*).
- **What it needs of the user:** make the shared file space's `knowledge/` subfolder
  (`EPICURUS_FILES_ROOT`) the Obsidian-synced vault and keep an Obsidian client syncing it
  on that machine.
- **Implementation gap (the follow-up):** a file-watcher (e.g. `watchfiles`) in the
  knowledge service that debounces filesystem changes under the vault and triggers an
  **incremental** re-index (the indexer is already hash/mtime-incremental, so a watch
  event over a synced folder is cheap). This is mechanism (c) applied to mount (a).

### (b) Git-based sync (Obsidian Git plugin ↔ a repo epicurus also tracks)

The user runs the community **Obsidian Git** plugin to commit/push the vault to a repo;
epicurus pulls that repo into `/vault` on a schedule (or a webhook) and re-indexes.

- **Pros:** works for a **headless/remote** host (no Obsidian process needed on the
  server); version history and conflict surfacing come for free; a clean one-way pull
  (repo → epicurus) sidesteps the two-writer problem if the editor page is read-only.
- **Cons:** more setup (a plugin, a repo, credentials/deploy key in OpenBao); pull is
  periodic, so it lags; two-way (epicurus edits → push back) reintroduces conflicts and
  needs a commit/push path; not real-time.
- **What it needs of the user:** install Obsidian Git, host a repo, give epicurus
  read (and, for two-way, write) access.

### (c) Standalone file-watch / sync bridge over a shared folder

A separate sidecar watches a shared folder and mirrors it into the vault.

- **Verdict:** **not worth it as a separate thing.** Its only useful half is the
  *watch-and-reindex* part, which belongs inside the knowledge service on top of mount
  (a). A standalone mirror adds a process and a second copy of the data for no gain over
  (a)+watcher or (b).

## Recommendation

1. **Adopt (a) as the primary path** and make it first-class by adding a
   **vault file-watcher → incremental re-index** to the knowledge service. This turns
   the already-supported bind-mount into a live sync for the common case (Obsidian +
   Sync on the same box) with the least new surface.
2. **Document (b) as the headless/remote recipe** (Obsidian Git → repo → scheduled pull
   → reindex), one-way pull first; defer two-way push.
3. **Drop (c)** as a standalone component.

This keeps epicurus indexing a plain markdown folder and never touches the proprietary
Sync protocol — Obsidian (or Git) owns syncing and conflict resolution; epicurus owns
indexing.

## Risks to handle in the implementation

- **Indexer churn.** Obsidian Sync can land many file events in a burst; the watcher
  must **debounce** (coalesce a quiet window) and rely on the incremental hash/mtime
  skip so unchanged files cost nothing. Ignore Obsidian's own `.obsidian/` config dir
  and `.trash/`.
- **Partial writes.** A sync may write a file in pieces; debounce + re-reading on the
  next event makes a torn read self-correct on the following pass.
- **Conflicts / two writers.** With (a), both Obsidian and the knowledge **editor page**
  (#130) can write the same file. Obsidian Sync resolves conflicts within its own
  ecosystem (it creates conflict copies); epicurus writes are last-write-wins at the
  filesystem and then sync out. Safest first cut: treat the editor as the secondary
  writer, or make the watched-vault mode **read-only** in the shell and let Obsidian be
  the sole author. Decide this explicitly in the follow-up.
- **Deletions.** The incremental indexer already purges vectors for files gone from the
  vault, so a sync-deletion is handled on the next pass.

## Follow-up

**Implemented in #232.** The knowledge service gained a debounced `watchfiles` watcher
(`VAULT_WATCH`) that drives an incremental re-index when the bind-mounted vault changes,
and the watched-vault mode is **read-only** so Obsidian stays the sole author — the
read/write-ownership decision is **ADR-0035**. See the setup recipes (same-host bind-mount
and headless Obsidian Git) in **[Keeping the knowledge vault in sync with Obsidian](obsidian-sync.md)**.
