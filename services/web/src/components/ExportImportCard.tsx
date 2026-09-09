/** Export & import (#867) — move one tenant from this epicurus to another, through the UI.
 *
 *  Two halves of one card, deliberately asymmetric. **Export** is a single button because it
 *  changes nothing: press it, watch the components tick over, download the archive.
 *  **Import** never applies what you hand it: the upload only *reads* the archive and comes
 *  back with a preview — what is in it, how each component grades against this install, and
 *  what it will not carry — and Apply is a second, deliberate press.
 *
 *  The card renders data, not decisions: every verdict, count, exclusion and "re-enter this"
 *  line comes from the core (ADR-0018). Both halves poll while a job is in flight and stop
 *  the moment it settles, so an idle Settings page makes no requests.
 *
 *  **A job outlives this page** (#877). The card used to hold a job id in component state
 *  and nothing else, so a reload mid-export orphaned the run: it finished staging, was
 *  never offered, and was swept a day later. The job list is now the source of truth — the
 *  card reads it on mount and re-attaches to the newest job of each kind, whatever this tab
 *  did or did not start. Deliberately not `localStorage`: the server's list is right on a
 *  second device and in a different browser, and a remembered id is right in neither.
 *
 *  **An import shows its work** (#893). Both halves now render the same component table: the
 *  upload reports how far the bytes have got, the apply ticks over sets → modules → files →
 *  the two rebuilds, and Apply is impossible — not merely refused — the moment anything is in
 *  flight. The one line the card does not compose is the "no embedding model" warning: that
 *  sentence is written by the core and rendered here verbatim.
 *
 *  **A settled job can be let go of** (#903). Jobs used to leave only through the retention
 *  sweep, which runs when the *next* job starts — so a failed import sat here with its report
 *  on screen for a day, and the only way to clear it was to start another job. Remove is
 *  offered on any job that is not running, in the import half and on every row of the job
 *  list; a running one is refused by the core (a delete is not a cancel) and so is not
 *  offered here either. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Download, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { Badge, Button, Card, Dot, Spinner } from "@/components/ui";
import { api, ApiError, ProxyError } from "@/lib/api";
import type {
  PortabilityComponent,
  PortabilityImportJob,
  PortabilityJobSummary,
  PortabilityPreviewComponent,
  PortabilityReport,
  PortabilitySecrets,
} from "@/lib/contracts";
import { relativeTime } from "@/lib/format";

const POLL_MS = 1_000;

/** Not `@/lib/format`'s `formatBytes`: that one answers `""` for 0 (right for an optional
 *  size chip, wrong inside "Download archive (…)"), and an empty archive is a real state. */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function stateTone(state: string): "ok" | "danger" | "accent" | "dim" {
  if (state === "included") return "ok";
  if (state === "failed") return "danger";
  if (state === "running") return "accent";
  return "dim";
}

function verdictTone(verdict: string): "ok" | "warn" | "danger" {
  if (verdict === "warning") return "warn";
  if (verdict === "refused") return "danger";
  return "ok";
}

/** The component table — one row per core set, module, and the file space. */
function ComponentRows({ components }: { components: PortabilityComponent[] }) {
  return (
    <ul className="flex flex-col gap-0.5 text-[11px] text-ink-dim">
      {components.map((component) => (
        <li key={`${component.kind}-${component.name}`} className="flex items-center gap-1.5">
          <Dot tone={stateTone(component.state)} />
          <span className="text-ink">{component.name}</span>
          {component.state === "included" && <span>· {component.count.toLocaleString()}</span>}
          {component.reason && <span className="text-warn">· {component.reason}</span>}
          {component.error && <span className="text-danger">· {component.error}</span>}
        </li>
      ))}
    </ul>
  );
}

/** "Remove" — forget one settled job and the archive staged for it (#903).
 *
 *  A failed import used to be permanent furniture: jobs leave the list only through the
 *  retention sweep, which runs when the *next* job starts, so the operator's only way to
 *  clear a failure was to cause another one and wait a day. Offered on both kinds because
 *  "Recent jobs" lists both, and never while a job is running — the core refuses that with a
 *  409 (a delete is not a cancel), and offering a button that cannot work is worse than not
 *  offering it. `onRemoved` is what lets the half that is *displaying* the job let go of it;
 *  invalidating the list alone would leave a removed job's report on screen. */
function RemoveJob({
  kind,
  jobId,
  onRemoved,
}: {
  kind: "export" | "import";
  jobId: string;
  onRemoved: (jobId: string) => void;
}) {
  const qc = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.removePortabilityJob(kind, jobId),
    onSuccess: () => {
      onRemoved(jobId);
      qc.removeQueries({ queryKey: [`portability-${kind}`, jobId] });
      qc.invalidateQueries({ queryKey: ["portability-jobs"] });
    },
  });
  return (
    <>
      <button
        type="button"
        className="inline-flex items-center gap-1 text-ink-faint underline hover:text-ink-dim disabled:opacity-50"
        disabled={remove.isPending}
        onClick={() => remove.mutate()}
      >
        <Trash2 size={11} />
        Remove
      </button>
      {remove.isError && (
        <span className="text-danger">· {(remove.error as Error).message}</span>
      )}
    </>
  );
}

/** `messaging/discord` → `discord`: the module's name is already the line's subject, so
 *  repeating it in every path turns "messaging — discord, telegram" into noise. */
function secretLabel(module: string, path: string): string {
  return path.startsWith(`${module}/`) ? path.slice(module.length + 1) : path;
}

/** "Re-enter these" — secrets are never in the archive, so the list is all we can carry. */
function SecretsNotice({ secrets }: { secrets: PortabilitySecrets }) {
  const modules = Object.entries(secrets.module_secrets ?? {});
  if (!secrets.provider_keys.length && !secrets.connected_accounts.length && !modules.length)
    return null;
  return (
    <p className="text-[11px] text-warn">
      Not carried (secrets never leave the vault):
      {secrets.provider_keys.length > 0 && (
        <> re-enter API keys for {secrets.provider_keys.join(", ")}.</>
      )}
      {secrets.connected_accounts.length > 0 && (
        <> reconnect {secrets.connected_accounts.join(", ")}.</>
      )}
      {modules.length > 0 && (
        <>
          {" "}
          Re-enter in module settings:{" "}
          {modules
            .map(([module, paths]) => `${module} — ${paths.map((p) => secretLabel(module, p)).join(", ")}`)
            .join("; ")}
          .
        </>
      )}
    </p>
  );
}

function ExportHalf({
  jobs,
  removed,
}: {
  jobs: PortabilityJobSummary[];
  removed: string[];
}) {
  const qc = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: () => api.startPortabilityExport(),
    onSuccess: (job) => {
      setJobId(job.id);
      qc.invalidateQueries({ queryKey: ["portability-jobs"] });
    },
  });

  // What this tab started, else the newest export the server knows about (the list is
  // newest-first). That second clause is the whole of #877: after a reload there is no
  // `jobId`, and without it a finished archive is never offered to anyone.
  // …unless it has since been removed from the list (#903): a job id this tab is still
  // holding must not outlive the row it names, or the half keeps polling a 404.
  const own = jobId !== null && !removed.includes(jobId) ? jobId : null;
  const activeId = own ?? jobs.find((entry) => entry.kind === "export")?.id ?? null;
  const summary = jobs.find((entry) => entry.id === activeId) ?? null;
  const job = useQuery({
    queryKey: ["portability-export", activeId],
    queryFn: () => api.portabilityExport(activeId as string),
    enabled: activeId !== null,
    refetchInterval: (query) => (query.state.data?.status === "running" ? POLL_MS : false),
  });

  const data = job.data;
  const running = data?.status === "running" || start.isPending;
  // Until the list has caught up with a job we just started, assume the archive we are
  // about to stage will be there — the alternative is hiding a live download link for a
  // second. A resumed job is only ever trusted to the server's answer.
  const archiveAvailable = summary ? summary.archive_available : own !== null;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" busy={running} onClick={() => start.mutate()}>
          <Download size={14} />
          {running ? "Exporting…" : "Export everything"}
        </Button>
        {data?.status === "ready" && archiveAvailable && (
          <a
            className="inline-flex items-center gap-2 rounded-(--radius-field) border border-accent/40 bg-accent-dim px-3.5 py-2 text-sm text-accent-strong"
            href={api.portabilityArchiveUrl(data.id)}
            download
          >
            <Download size={14} />
            Download archive ({formatBytes(data.size_bytes)})
          </a>
        )}
      </div>
      {start.isError && (
        <p className="text-sm text-danger">{(start.error as Error).message}</p>
      )}
      {data?.status === "failed" && <p className="text-sm text-danger">{data.error}</p>}
      {data?.status === "ready" && !archiveAvailable && (
        <p className="text-[11px] text-warn">
          That archive has been cleaned up — staging is a cache, not storage. Export again to
          get a fresh one.
        </p>
      )}
      {data && data.progress.length > 0 && <ComponentRows components={data.progress} />}
      {data?.manifest && <SecretsNotice secrets={data.manifest.secrets} />}
    </div>
  );
}

function PreviewRows({ components }: { components: PortabilityPreviewComponent[] }) {
  return (
    <table className="w-full text-left text-[11px]">
      <thead className="text-ink-faint">
        <tr>
          <th className="py-1 font-normal">component</th>
          <th className="py-1 font-normal">records</th>
          <th className="py-1 font-normal">verdict</th>
        </tr>
      </thead>
      <tbody>
        {components.map((component) => (
          <tr key={`${component.kind}-${component.name}`} className="border-t border-edge">
            <td className="py-1 text-ink">{component.name}</td>
            <td className="py-1 text-ink-dim">{component.records.toLocaleString()}</td>
            <td className="py-1">
              <Badge tone={verdictTone(component.verdict)}>{component.verdict}</Badge>
              {component.detail && (
                <span className="ml-1 text-ink-dim">{component.detail}</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ReportView({ report }: { report: PortabilityReport }) {
  return (
    <div className="flex flex-col gap-2">
      <ul className="flex flex-col gap-0.5 text-[11px] text-ink-dim">
        {report.components.map((component) => (
          <li key={`${component.kind}-${component.name}`} className="flex items-center gap-1.5">
            <Dot tone={stateTone(component.state)} />
            <span className="text-ink">{component.name}</span>
            {component.state === "included" ? (
              <span>
                · {component.created} new · {component.updated} updated · {component.skipped}{" "}
                unchanged
              </span>
            ) : (
              <span className="text-warn">· {component.reason ?? component.error}</span>
            )}
          </li>
        ))}
      </ul>
      <p className="text-[11px] text-ink-dim">
        Files: {report.files.written} written · {report.files.skipped} already identical
        {report.files.conflicts.length > 0 && (
          <span className="text-warn">
            {" "}
            · {report.files.conflicts.length} left alone because they differ here
          </span>
        )}
      </p>
      <p className="text-[11px] text-ink-dim">
        Rebuilt afterwards: file index{" "}
        {report.rescan_error ? (
          <span className="text-danger">failed ({report.rescan_error})</span>
        ) : (
          `${report.rescan_forced ? "force-re-scanned" : "re-scanned"} (${
            report.rescan_entries ?? 0
          } entries)`
        )}
        {" · "}
        {report.reembed_error ? (
          <span className="text-danger">re-embed failed ({report.reembed_error})</span>
        ) : (
          `re-embed asked of ${report.reembed.length} module(s)${
            report.embedding_model ? ` with ${report.embedding_model}` : ""
          }`
        )}
      </p>
      {/* The core's own sentence, rendered verbatim (ADR-0018). It is here because the
          fan-out above cannot say it: every module accepts the re-embed and fails minutes
          later, long after this report is written. */}
      {report.embedding_note && (
        <p className="flex items-start gap-1.5 text-[11px] text-warn">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          {report.embedding_note}
        </p>
      )}
      <SecretsNotice secrets={report.reenter_secrets} />
    </div>
  );
}

/** A core refusal (413 over PORTABILITY_MAX_ARCHIVE_MB, 400 not a readable archive) arrives
 *  as an `ApiError` with the core's own sentence. Anything else means the request never got
 *  a core answer at all — in practice a proxy in front of it refusing the body (#887: the
 *  web image's nginx capped every `/platform/` upload at 12 MB, so a real archive died with
 *  a bare "Failed to fetch"). That refusal reaches us either as a network error (the
 *  connection dropped mid-body) or as a `ProxyError` (its HTML 413 arrived, carrying no core
 *  sentence); both get the same naming, because the symptom names nothing. */
function uploadFailure(error: unknown): string {
  const neverReached = (cause: string) =>
    `The archive never reached the core (${cause}). A proxy in front of it may cap the request size — see the web service docs.`;
  if (error instanceof ProxyError) return neverReached(`HTTP ${error.status}`);
  if (error instanceof ApiError) return error.detail;
  return neverReached(error instanceof Error ? error.message : String(error));
}

/** How far the archive's bytes have got — the half of an import that used to be a busy button.
 *
 *  `fetch` reports nothing at all until the response arrives, so a ten-minute upload of a
 *  multi-gigabyte archive looked exactly like a hung one; the request is made over `XMLHttp-
 *  Request` for that one reason (see `api.uploadPortabilityArchive`). A browser that will not
 *  commit to a total gives `null`, which is said as "uploading…" rather than dressed up as a
 *  percentage nothing measured. */
function UploadProgress({ fraction }: { fraction: number | null }) {
  const percent = fraction === null ? null : Math.round(fraction * 100);
  return (
    <div className="flex flex-col gap-1" data-testid="portability-upload-progress">
      <p className="flex items-center gap-2 text-[11px] text-ink-dim">
        <Spinner />
        {percent === null ? "Uploading…" : `Uploading… ${percent}%`}
      </p>
      {percent !== null && (
        <div
          className="h-1 w-full overflow-hidden rounded-full bg-edge"
          role="progressbar"
          aria-label="Archive upload"
          aria-valuenow={percent}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="h-full bg-accent" style={{ width: `${percent}%` }} />
        </div>
      )}
    </div>
  );
}

function ImportHalf({
  jobs,
  removed,
  onRemoved,
}: {
  jobs: PortabilityJobSummary[];
  removed: string[];
  onRemoved: (jobId: string) => void;
}) {
  const qc = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [job, setJob] = useState<PortabilityImportJob | null>(null);
  // null while nothing is uploading; a 0–1 fraction, or `null` inside the tuple when the
  // browser will not commit to a total (`lengthComputable: false` — a real state, and not 0%).
  const [sent, setSent] = useState<{ fraction: number | null } | null>(null);

  // Both mutations write their answer into the *query cache* as well as component state.
  // That is the whole of the double-apply fix (#893): the polled query used to keep serving
  // its stale `staged` copy until the next tick, so the card still offered Apply on a job
  // that was already running and a second press hit the core's 409. Seeding the cache with
  // the fresh answer makes the press impossible rather than merely refused.
  const settled = (next: PortabilityImportJob) => {
    setJob(next);
    qc.setQueryData(["portability-import", next.id], next);
    qc.invalidateQueries({ queryKey: ["portability-jobs"] });
  };
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadPortabilityArchive(file, (f) => setSent({ fraction: f })),
    onMutate: () => setSent({ fraction: 0 }),
    onSettled: () => setSent(null),
    onSuccess: settled,
  });
  const apply = useMutation({
    mutationFn: (jobId: string) => api.applyPortabilityImport(jobId),
    onSuccess: settled,
  });
  // This tab's own job, else the newest import on the server — so a reload lands back on
  // the preview it was about to apply, or the report of the apply it started.
  // …unless it has since been removed (#903). Dropping it here rather than only invalidating
  // the list is what makes Remove *clear the card*: this tab's own copy is the thing on
  // screen, and a report whose job no longer exists would otherwise stay up until a reload.
  const own = job !== null && !removed.includes(job.id) ? job : null;
  const activeId = own?.id ?? jobs.find((entry) => entry.kind === "import")?.id ?? null;
  const polled = useQuery({
    queryKey: ["portability-import", activeId],
    queryFn: () => api.portabilityImport(activeId as string),
    // Fetch once to re-attach to a job this tab did not start; after that, only while the
    // apply is actually in flight (an upload's answer is already the whole preview).
    enabled: activeId !== null && (own === null || own.status === "running"),
    refetchInterval: (query) => (query.state.data?.status === "running" ? POLL_MS : false),
  });

  // The polled copy wins once it exists: it is the one that grows a report. `settled` seeds
  // it, so "the polled copy" is never older than the last answer this tab received.
  const current = polled.data ?? own;
  const preview = current?.preview ?? null;
  const busy = upload.isPending || apply.isPending || current?.status === "running";
  // Apply is offered only for a job that is still staged, and never while anything is in
  // flight — including the optimistic window between the press and the mutation resolving.
  const canApply = current?.status === "staged" && preview?.compatible === true && !busy;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {/* eslint-disable-next-line no-restricted-syntax -- hidden native file picker,
            opened by the button below; not a styled field (same shape as AttachMenu). */}
        <input
          ref={fileInput}
          type="file"
          accept=".gz,.tgz,application/gzip"
          className="hidden"
          data-testid="portability-file"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) upload.mutate(file);
            event.target.value = "";
          }}
        />
        <Button
          variant="outline"
          busy={upload.isPending}
          disabled={busy}
          onClick={() => fileInput.current?.click()}
        >
          <Upload size={14} />
          Choose an archive…
        </Button>
        {preview && current?.status === "staged" && (
          <Button
            variant="primary"
            busy={apply.isPending}
            disabled={!canApply}
            onClick={() => apply.mutate(current.id)}
          >
            Apply import
          </Button>
        )}
        {/* A settled import can be cleared straight from the half that is showing it (#903) —
            a failed one especially, since its report is what the operator is looking at. */}
        {current && !busy && (
          <span className="flex items-center gap-1.5 text-[11px] text-ink-dim">
            <RemoveJob kind="import" jobId={current.id} onRemoved={onRemoved} />
          </span>
        )}
      </div>
      {sent !== null && <UploadProgress fraction={sent.fraction} />}
      {upload.isError && <p className="text-sm text-danger">{uploadFailure(upload.error)}</p>}
      {apply.isError && <p className="text-sm text-danger">{(apply.error as Error).message}</p>}
      {current?.status === "failed" && <p className="text-sm text-danger">{current.error}</p>}
      {preview && (
        <div className="flex flex-col gap-2">
          <p className="text-[11px] text-ink-dim">
            From tenant <span className="font-mono text-ink">{preview.manifest.tenant}</span> ·
            core {preview.manifest.core_app_version} · {preview.manifest.created_at.slice(0, 16).replace("T", " ")}
          </p>
          {!preview.compatible && (
            <p className="flex items-center gap-1.5 text-sm text-danger">
              <AlertTriangle size={14} />
              {preview.refusal}
            </p>
          )}
          <PreviewRows components={preview.components} />
          {preview.manifest.exclusions.length > 0 && (
            <details className="text-[11px] text-ink-dim">
              <summary className="cursor-pointer">
                Not in this archive ({preview.manifest.exclusions.length})
              </summary>
              <ul className="mt-1 flex flex-col gap-0.5">
                {preview.manifest.exclusions.map((exclusion) => (
                  <li key={exclusion.component}>
                    <span className="text-ink">{exclusion.component}</span> — {exclusion.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <SecretsNotice secrets={preview.manifest.secrets} />
        </div>
      )}
      {current?.status === "running" && (
        <p className="flex items-center gap-2 text-[11px] text-ink-dim">
          <Spinner /> Applying…
        </p>
      )}
      {/* The same table the export half uses, on the same data (#893). It stays up after the
          job settles: the last thing an operator watched tick over is the first thing they
          look back at when the report says a component was skipped. */}
      {current && current.status !== "staged" && current.progress.length > 0 && (
        <ComponentRows components={current.progress} />
      )}
      {current?.report && current.status === "done" && <ReportView report={current.report} />}
    </div>
  );
}

function jobTone(status: string): "ok" | "danger" | "accent" | "dim" {
  if (status === "ready" || status === "done") return "ok";
  if (status === "failed") return "danger";
  if (status === "running") return "accent";
  return "dim";
}

/** The jobs behind the two halves — the history a reload can still reach into.
 *
 *  The halves already show the newest of each kind in full; this is the rest, so a second
 *  export started an hour ago is still downloadable rather than merely swept. */
function RecentJobs({
  jobs,
  onRemoved,
}: {
  jobs: PortabilityJobSummary[];
  onRemoved: (jobId: string) => void;
}) {
  if (jobs.length === 0) return null;
  return (
    <details className="text-[11px] text-ink-dim" data-testid="portability-jobs">
      <summary className="cursor-pointer">Recent jobs ({jobs.length})</summary>
      <ul className="mt-1 flex flex-col gap-0.5">
        {jobs.map((job) => (
          <li key={job.id} className="flex flex-wrap items-center gap-1.5">
            <Dot tone={jobTone(job.status)} />
            <span className="text-ink">{job.kind}</span>
            <span>· {job.status}</span>
            <span>· {relativeTime(new Date(job.created_at))}</span>
            {job.kind === "export" && job.status === "ready" && (
              job.archive_available ? (
                <a className="text-accent-strong underline" href={api.portabilityArchiveUrl(job.id)} download>
                  download ({formatBytes(job.size_bytes)})
                </a>
              ) : (
                <span className="text-warn">· archive cleaned up</span>
              )
            )}
            {job.status !== "running" && (
              <RemoveJob kind={job.kind} jobId={job.id} onRemoved={onRemoved} />
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

export function ExportImportCard() {
  // Read on mount, before anything is clicked: this is how the card finds the job it (or
  // another tab, or another device) started. It polls only while something is in flight, so
  // an idle Settings page settles back to silence.
  const jobs = useQuery({
    queryKey: ["portability-jobs"],
    queryFn: () => api.portabilityJobs(),
    refetchInterval: (query) =>
      query.state.data?.some((job) => job.status === "running") ? POLL_MS : false,
  });
  // What this tab has removed, held here rather than left to the list's next refetch (#903).
  // The delete answers 204 and the invalidated list arrives a moment later; without this the
  // row — and, worse, the report the half is rendering from its own copy — would linger in
  // between, so a press would look like it had done nothing.
  const [removed, setRemoved] = useState<string[]>([]);
  const forget = (jobId: string) => setRemoved((ids) => [...ids, jobId]);
  const list = (jobs.data ?? []).filter((job) => !removed.includes(job.id));

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Export &amp; import</h3>
      <p className="mb-3 text-sm text-ink-dim">
        Take everything with you: your chats, memory, playbooks, automations, preferences,
        files, and each module&rsquo;s own data, in one archive you can read. Importing is
        additive — it adds what is missing and never deletes anything, so applying the same
        archive twice changes nothing.
      </p>
      <div className="flex flex-col gap-4">
        <ExportHalf jobs={list} removed={removed} />
        <div className="border-t border-edge pt-3">
          <ImportHalf jobs={list} removed={removed} onRemoved={forget} />
        </div>
        {list.length > 0 && (
          <div className="border-t border-edge pt-3">
            <RecentJobs jobs={list} onRemoved={forget} />
          </div>
        )}
      </div>
    </Card>
  );
}
