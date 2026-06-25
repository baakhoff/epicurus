/**
 * The turn-activity surface: a warming readiness bar (#122), a "thinking" indicator while
 * the first token is pending, and a step-by-step process timeline of the agent's thinking
 * and tool calls (#121, ADR-0041). The shell owns this rendering — modules supply only the
 * event data carried on the chat stream (ADR-0018 / ADR-0027). The timeline renders both the
 * live turn and, from the message's persisted activity, a reopened past turn.
 */
import { Brain, Check, ChevronDown, Loader2, Wrench, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Spinner, Tooltip, cn } from "@/components/ui";
import { readinessProgress, readinessSummary, toolLabel } from "@/lib/activity";
import type { Readiness } from "@/lib/contracts";
import type { ActivityItem, ToolRun } from "@/stores/chat";

/* ── thinking (model warm, first token pending) ─────────────────────────── */

export function ThinkingIndicator() {
  // Icon-only (#334): the label lives in the tooltip so the cue stays compact in the chat.
  return (
    <Tooltip label="Thinking…">
      <span
        className="inline-flex items-center text-ink-dim"
        aria-live="polite"
        aria-label="Thinking…"
      >
        <Loader2 size={15} className="animate-spin" />
      </span>
    </Tooltip>
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
    <div>
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
    </div>
  );
}

/** The model's chain-of-thought, collapsible, inside the timeline (ADR-0041). */
function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="py-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 py-1 text-left text-xs text-ink-dim"
        aria-expanded={open}
      >
        <span className="flex size-4 shrink-0 items-center justify-center">
          <Brain size={13} className="text-accent" />
        </span>
        <span>Thinking</span>
        <ChevronDown
          size={11}
          className={cn("ml-auto shrink-0 text-ink-faint transition-transform", open && "rotate-180")}
        />
      </button>
      {open && (
        <pre className="mb-1 ml-6 max-h-56 overflow-auto whitespace-pre-wrap rounded-(--radius-field) border border-edge bg-surface-2 p-2 text-[11px] leading-relaxed text-ink-dim">
          {text}
        </pre>
      )}
    </div>
  );
}

export function ProcessTimeline({
  items,
  collapsed = false,
}: {
  /** The turn's *process* — thinking blocks and tool steps in chronological order (#300). */
  items: ActivityItem[];
  /** Fold to the summary header once the answer is flowing; the reader can still toggle. */
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(true);
  const userToggled = useRef(false);
  // Follow the auto-collapse cue (answer started) until the reader takes control.
  useEffect(() => {
    if (!userToggled.current) setOpen(!collapsed);
  }, [collapsed]);

  if (items.length === 0) return null;
  const toolCount = items.filter((i) => i.kind === "tool").length;
  const running = items.some((i) => i.kind === "tool" && i.run.status === "running");
  const stepLabel =
    toolCount > 0 ? `${toolCount} step${toolCount > 1 ? "s" : ""}` : "Thought process";
  // Icon-only summary (#334): the wordy label ("Working…", "N steps", "Thought process")
  // moves into a hover tooltip so the chat stays uncluttered. The compact toggle sits on its
  // own (tooltips can't escape an `overflow-hidden` box) with the step list in a panel below.
  const label = running ? "Working…" : stepLabel;
  const toggle = () => {
    userToggled.current = true;
    setOpen((v) => !v);
  };

  return (
    <div className="my-2">
      <Tooltip label={label}>
        <button
          type="button"
          onClick={toggle}
          aria-label={label}
          aria-expanded={open}
          className="inline-flex items-center gap-1.5 rounded-(--radius-field) px-1.5 py-1 text-xs text-ink-dim transition-colors hover:bg-surface-2 hover:text-ink"
        >
          {running ? <Spinner className="size-3" /> : toolCount > 0 ? <Wrench size={13} /> : <Brain size={13} />}
          <ChevronDown size={11} className={cn("transition-transform", open && "rotate-180")} />
        </button>
      </Tooltip>
      {open && (
        <div className="mt-1 flex flex-col overflow-hidden rounded-(--radius-field) border border-edge px-2.5 py-1">
          {items.map((item, i) =>
            item.kind === "thinking" ? (
              <ThinkingBlock key={i} text={item.text} />
            ) : (
              <TimelineStep key={i} run={item.run} />
            ),
          )}
        </div>
      )}
    </div>
  );
}
