# Export & import

Take everything with you. epicurus can write your whole tenant — chats, memory, playbooks,
automations, preferences, files, and each module's own data — into a single `.tar.gz`, and
read one back into another installation. Both halves live in **Settings → Export & import**.

This is not the same thing as [Backup and restore](../infrastructure/backup-and-restore.md).
That images *this deployment's* volumes so you can put the same machine back the way it was;
this moves *your data* to a different one. Keep using backups for disaster recovery.

## Export

Press **Export everything**. The card lists every component as it goes:

| State | Means |
| --- | --- |
| **included** | written to the archive, with a record count |
| **skipped** | deliberately not carried, with the reason beside it |
| **failed** | something went wrong for that component only |

A module that is turned off, unreachable, or simply has no data worth carrying is *skipped*,
and the export carries on. Losing your conversations because the mail container happened to
be restarting would be an absurd way to lose your conversations.

**You can close the tab.** The job runs on the server, not in the browser, and the card
finds it again when you come back — on a reload, on a second device, in a different browser.
A run still going carries on where you left it; a finished one still has its **Download
archive** link waiting. **Recent jobs** at the foot of the card lists the ones before it,
each with its own download while its archive lasts. (Archives are cleaned up after a while —
see the table at the end. A job whose archive has gone says so, and the answer is to export
again.)

When the job is ready a **Download archive** link appears. The archive is a plain gzipped
tar you can open with tools you already have:

```
manifest.json                 what this archive is, and what it deliberately omits
core/<set>.ndjson             the assistant's own data, one file per group
modules/<name>.ndjson         each module's data, as the module wrote it
files/<path>                  your file space, exactly as it sits on disk
```

Everything inside is JSON, one record per line. Read it before you move it.

### What an export does not contain

**Secrets never leave the vault.** Provider API keys, connected-account tokens and the bot
tokens behind your **chat bridges** live in OpenBao and are not in the archive at any
strength. What the archive *does* carry is the list of their names, so the import can tell
you exactly what to re-enter, what to reconnect, and which bridges to set up again — a
module like `messaging` stores nothing else at all, so its whole presence in an archive is
that one line.

**Derived data is not carried, because it is rebuilt.** Search indexes and every embedding
vector are specific to the model that produced them, so copying them to a machine that may
run a different embedding model would be worse than useless. The import re-scans your files
and asks every module to re-embed, which is what actually restores search.

**Operational state is not carried.** Queued background work, run history, paused turns
waiting on an answer, the automations kill switch, browser push subscriptions — all of it
describes the machine you are leaving, not you. The archive's `manifest.json` lists every
omission with its reason.

## Import

Choosing an archive **uploads and reads it — it applies nothing**. What comes back is a
preview:

* where the archive came from, and when
* every component with its record count and a verdict
* what the archive deliberately leaves behind
* the keys and accounts you will need to re-enter afterwards

| Verdict | Means |
| --- | --- |
| **ok** | this installation can read it |
| **warning** | written by an older version of that component's format; it will be upgraded on the way in |
| **refused** | this installation cannot read it — a newer format, or a module that is not installed here. That component is skipped; everything else still applies |

Only if the whole archive was written in a **format version** this build does not know is
the import refused outright — there is nothing useful to salvage from guessing.

Press **Apply import** when the preview looks right.

### Importing is additive

An import **adds; it never deletes**. A record that is missing here is created; one that is
already here and identical is left alone; one that differs is updated. Nothing that is here
and absent from the archive is touched.

That has two useful consequences:

* **Applying the same archive twice does nothing.** Run it again if you are not sure it
  finished — the second pass reports everything as unchanged.
* **You can import into a machine that is already in use.** It merges.

One deliberate exception, in your favour: a **file** that exists here with different
contents is *never* overwritten. It is named in the report as a conflict, and you decide.

### After an apply

The report shows what each component did, which files were written, and the two rebuilds the
import runs for you:

1. a **forced re-scan** of your file space, rebuilding the Files index and search;
2. the **re-embed** fan-out, asking each module to rebuild its vectors with *this*
   installation's embedding model.

Then finish the move by hand: **re-enter the API keys**, **reconnect the accounts**, and
**reconnect the chat bridges** the report names (Settings → Chat bridges). Nothing else will
do it for you — that is the point of keeping them out of the archive.

## Moving from one epicurus to another

1. On the old machine: Settings → Export & import → **Export everything** → download.
2. Stand the new one up ([Installation](installation.md)) and open its Settings.
3. **Choose an archive**, read the preview, **Apply import**.
4. Re-enter the API keys, reconnect the accounts, and reconnect the chat bridges the report
   lists.
5. Give the re-embed a few minutes before judging search.

## Limits and knobs

Defaults are fine for a personal install; see [config reference](../reference/config.md) for
the full table.

| Setting | Default | What it does |
| --- | --- | --- |
| `PORTABILITY_MAX_FILE_MB` | `512` | Largest single file an export will carry. Anything bigger is left out and **named** in the job, so you can move it yourself. `0` = no limit. |
| `PORTABILITY_MAX_ARCHIVE_MB` | `4096` | Largest archive the import will accept. |
| `PORTABILITY_RETENTION_HOURS` | `24` | How long a finished archive stays downloadable before it is cleaned up. |
| `PORTABILITY_STAGING_DIR` | `/tmp/epicurus-portability` | Scratch space where archives are built. It is a cache, not storage: a restart may empty it, and a download of a cleaned-up archive tells you to export again rather than pretending. |
