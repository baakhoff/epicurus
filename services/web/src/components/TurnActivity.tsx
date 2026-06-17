/**
 * The live-turn activity surface: a warming readiness bar (#122), a "thinking"
 * indicator while the first token is pending, and a step-by-step process timeline of the
 * agent's tool calls (#121). The shell owns this rendering — modules supply only the event
 * data carried on the chat stream (ADR-0018 / ADR-0027).
 */
import { Check, ChevronDown, Loader2, Wrench, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Spinner, cn } from "@/components/ui";
import { readinessProgress, readinessSummary, toolLabel } from "@/lib/activity";
import type { Readiness } from "@/lib/contracts";
import type { ToolRun } from "@/stores/chat";

/* ── thinking (model warm, first token pending) ─────────────────────────── */

export function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-2 text-sm text-ink-dim" aria-live="polite">
      <Loader2 size={13} className="animate-spin" />
      <span>Thinking…</span>
    </div>
  );
}

/* ── readiness / warming bar (#122) ─────────────────────────────────────── */

export function ReadinessBar({ readiness }: { readiness: Readiness }) {
  const pct = Math.round(readinessProgress(readiness) * 100);
  return (
    <div className="flex max-w-sm flex-col gap-1.5" aria-live="polite">
      <div className="flex items-center gap-2 text-sm text-ink-dim">
        <Loader2 size={13} className="animate-spin" />
        <span>{readinessSummary(readiness)}</span>
      </div>
      <div
        className="h-1 w-full overflow-hidden rounded-full bg-surface-2"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-500 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex flex-wrap gap-1.5">
        {readiness.components.map((component) => (
          <span
            key={component.name}
            className={cn(
              "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] leading-4",
              component.ready ? "border-ok/40 text-ok" : "border-edge text-ink-faint",
            )}
          >
            {component.ready ? <Check size={10} /> : <Spinner className="size-2.5" />}
            {component.detail || component.name}
          </span>
        ))}
      </div>
    </div>
  );
}

/* ── process timeline (#121) ────────────────────────────────────────────── */

function StatusIcon({ status }: { status: ToolRun["status"] }) {
  if (status === "running") return <Spinner className="size-3" />;
  if (status === "ok") return <Check size={13} className="text-ok" />;
  return <X size={13} className="text-danger" />;
}

function TimelineStep({ run }: { run: ToolRun }) {
  const [open, setOpen] = useState(false);
  const hasDetail = Boolean(run.detail);
  return (
    <li>
      <button
        type="button"
        onClick={() => hasDetail && setOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 py-1 text-left text-xs",
          hasDetail ? "cursor-pointer" : "cursor-default",
        )}
        aria-expanded={hasDetail ? open : undefined}
      >
        <span className="flex size-4 shrink-0 items-center justify-center">
          <StatusIcon status={run.status} />
        </span>
        <span
          className={cn("truncate", run.status === "error" ? "text-danger" : "text-ink-dim")}
          title={run.tool}
        >
          {toolLabel(run.tool)}
        </span>
        {hasDetail && (
          <ChevronDown
            size={11}
            className={cn("ml-auto shrink-0 text-ink-faint transition-transform", open && "rotate-180")}
          />
        )}
      </button>
      {open && run.detail && (
        <pre className="mb-1 ml-6 max-h-40 overflow-auto rounded-(--radius-field) border border-edge bg-surface-2 p-2 font-mono text-[11px] leading-relaxed text-ink-dim">
          {run.detail}
        </pre>
      )}
    </li>
  );
}

export function ProcessTimeline({
  runs,
  collapsed = false,
}: {
  runs: ToolRun[];
  /** Fold to the summary header once the answer is flowing; the reader can still toggle. */
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(true);
  const userToggled = useRef(false);
  // Follow the auto-collapse cue (answer started) until the reader takes control.
  useEffect(() => {
    if (!userToggled.current) setOpen(!collapsed);
  }, [collapsed]);

  if (runs.length === 0) return null;
  const running = runs.some((run) => run.status === "running");
  const toggle = () => {
    userToggled.current = true;
    setOpen((v) => !v);
  };

  return (
    <div className="my-2 overflow-hidden rounded-(--radius-field) border border-edge">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs text-ink-dim hover:text-ink"
        aria-expanded={open}
      >
        {running ? <Spinner className="size-3" /> : <Wrench size={12} />}
        <span>{running ? "Working…" : `${runs.length} step${runs.length > 1 ? "s" : ""}`}</span>
        <ChevronDown size={12} className={cn("ml-auto transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <ol className="flex flex-col border-t border-edge px-2.5 pb-1">
          {runs.map((run, i) => (
            <TimelineStep key={i} run={run} />
          ))}
        </ol>
      )}
    </div>
  );
}
