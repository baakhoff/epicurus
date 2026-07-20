/**
 * The Observability screen — system health plus the core's live feeds.
 *
 * Three tabs: **Logs** (ADR-0031's structured log console), **Events** (the module event
 * spine's raw tail — what the modules announced happened), and **Automation runs** (#669 —
 * what the engine did about it: fire → filter verdict → run → sinks, skips included).
 *
 * Each console owns its own SSE subscription and its own state. That separation is why
 * they are components rather than branches inside one screen: two feeds sharing one
 * function body share their entries, filters, and reconnect flag, and immediately collide.
 * The reconnect loop itself lives in `useSseFeed`.
 */
import { Check, WifiOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { EntityRefChip } from "@/components/EntityRef";
import { Badge, Button, Select, Spinner, Tabs, TextInput, cn } from "@/components/ui";
import type { TabSpec } from "@/components/ui";
import { api, eventStream, logStream, runStream } from "@/lib/api";
import type {
  Automation,
  AutomationRun,
  LogEntry,
  ModuleEvent,
  Readiness,
  ReadinessComponent,
} from "@/lib/contracts";
import { useSseFeed } from "@/lib/useSseFeed";

/* ── constants ───────────────────────────────────────────────────────────── */

const MAX_DISPLAY = 500;

type LevelFilter = "debug" | "info" | "warning" | "error";
const LEVEL_OPTIONS: { value: LevelFilter; label: string }[] = [
  { value: "debug", label: "All" },
  { value: "info", label: "Info+" },
  { value: "warning", label: "Warning+" },
  { value: "error", label: "Error only" },
];

type TabId = "logs" | "events" | "runs";
const TABS: TabSpec<TabId>[] = [
  { id: "logs", label: "Logs" },
  { id: "events", label: "Events" },
  { id: "runs", label: "Automation runs" },
];

/* ── colour helpers ──────────────────────────────────────────────────────── */

function levelTextClass(level: string): string {
  switch (level) {
    case "debug":
      return "text-ink-faint";
    case "info":
      return "text-ink";
    case "warning":
      return "text-warn";
    case "error":
    case "critical":
      return "text-danger";
    default:
      return "text-ink-dim";
  }
}

function levelBadgeTone(level: string): "dim" | "accent" | "ok" | "warn" | "danger" {
  switch (level) {
    case "debug":
      return "dim";
    case "info":
      return "accent";
    case "warning":
      return "warn";
    case "error":
    case "critical":
      return "danger";
    default:
      return "dim";
  }
}

/** An ISO timestamp as a readable wall-clock time; falls back to the raw string. */
function shortTime(iso: string, withMillis = true): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      ...(withMillis ? { fractionalSecondDigits: 3 as const } : {}),
      hour12: false,
    });
  } catch {
    return iso;
  }
}

/* ── health row ──────────────────────────────────────────────────────────── */

function HealthRow({ readiness }: { readiness: Readiness | null }) {
  if (!readiness) {
    return (
      <div className="flex items-center gap-2 text-xs text-ink-faint">
        <Spinner className="size-3" />
        <span>Checking system health…</span>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {readiness.components.map((c: ReadinessComponent) => (
        <span
          key={c.name}
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] leading-4",
            c.ready ? "border-ok/40 text-ok" : "border-edge text-ink-faint",
          )}
        >
          {c.ready ? <Check size={10} /> : <Spinner className="size-2.5" />}
          {c.detail || c.name}
        </span>
      ))}
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] leading-4",
          readiness.ready ? "border-ok/40 text-ok" : "border-edge text-ink-faint",
        )}
      >
        {readiness.ready ? <Check size={10} /> : <Spinner className="size-2.5" />}
        {readiness.power}
      </span>
    </div>
  );
}

/* ── shared console chrome ───────────────────────────────────────────────── */

/** The toolbar every console carries: its filters, then a status/count/clear cluster. */
function ConsoleBar({
  children,
  count,
  noun,
  disconnected,
  onClear,
}: {
  children: React.ReactNode;
  count: number;
  noun: string;
  disconnected: boolean;
  onClear: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-2">
      {children}
      <span className="ml-auto flex items-center gap-2 text-xs text-ink-faint">
        {disconnected && (
          <span className="flex items-center gap-1 text-warn">
            <WifiOff size={12} />
            Reconnecting…
          </span>
        )}
        <span>
          {count} {count === 1 ? noun : `${noun}s`}
        </span>
        <Button variant="ghost" onClick={onClear} className="text-xs">
          Clear
        </Button>
      </span>
    </div>
  );
}

/**
 * A scrolling feed list that follows the tail — but only while the reader is already at
 * it. Scrolling up to read something must not be yanked back by the next arriving entry.
 */
function FeedList<T>({
  entries,
  empty,
  label,
  render,
}: {
  entries: T[];
  empty: string;
  label: string;
  render: (entry: T, index: number) => React.ReactNode;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const shouldAutoScroll = useCallback(() => {
    const list = listRef.current;
    if (!list) return true;
    const threshold = 120; // px from the bottom
    return list.scrollHeight - list.scrollTop - list.clientHeight < threshold;
  }, []);

  useEffect(() => {
    if (shouldAutoScroll()) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries, shouldAutoScroll]);

  return (
    <ul
      ref={listRef}
      className="min-h-0 flex-1 divide-y divide-edge overflow-y-auto px-4 py-2"
      aria-label={label}
      aria-live="polite"
      aria-atomic={false}
    >
      {entries.length === 0 ? (
        <li className="flex items-center justify-center py-10 text-sm text-ink-faint">{empty}</li>
      ) : (
        entries.map(render)
      )}
      <div ref={bottomRef} />
    </ul>
  );
}

/* ── logs console ────────────────────────────────────────────────────────── */

function LogRow({ entry }: { entry: LogEntry }) {
  const hasContext = Object.keys(entry.context).length > 0;
  const [expanded, setExpanded] = useState(false);

  return (
    <li className={cn("flex flex-col gap-0.5 py-1", levelTextClass(entry.level))}>
      <div className="flex flex-wrap items-baseline gap-2 font-mono text-[11px]">
        <span className="shrink-0 text-ink-faint">{shortTime(entry.ts)}</span>
        <Badge tone={levelBadgeTone(entry.level)} className="shrink-0 font-mono uppercase">
          {entry.level === "critical" ? "CRIT" : entry.level.toUpperCase().slice(0, 4)}
        </Badge>
        {entry.service && <span className="shrink-0 text-ink-dim">{entry.service}</span>}
        <span className="flex-1 break-all">{entry.message}</span>
        {hasContext && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto shrink-0 rounded px-1 text-ink-faint hover:text-ink"
            aria-label={expanded ? "Collapse context" : "Expand context"}
          >
            {expanded ? "▲" : "▼"}
          </button>
        )}
      </div>
      {expanded && hasContext && (
        <pre className="ml-2 overflow-auto rounded-(--radius-field) border border-edge bg-surface-2 p-2 font-mono text-[10px] leading-relaxed text-ink-dim">
          {JSON.stringify(entry.context, null, 2)}
        </pre>
      )}
    </li>
  );
}

function LogsConsole() {
  const [levelFilter, setLevelFilter] = useState<LevelFilter>("info");
  const [serviceFilter, setServiceFilter] = useState("");

  const { entries, disconnected, clear } = useSseFeed<LogEntry>(
    (signal) => logStream(levelFilter, serviceFilter || undefined, signal),
    `${levelFilter} ${serviceFilter}`,
    MAX_DISPLAY,
  );

  return (
    <>
      <ConsoleBar count={entries.length} noun="entry" disconnected={disconnected} onClear={clear}>
        <Select
          size="sm"
          value={levelFilter}
          onChange={(e) => setLevelFilter(e.target.value as LevelFilter)}
          aria-label="Minimum log level"
        >
          {LEVEL_OPTIONS.map(({ value, label }) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <TextInput
          value={serviceFilter}
          onChange={(e) => setServiceFilter(e.target.value)}
          placeholder="Filter by service prefix…"
          className="max-w-64 text-xs"
          aria-label="Service prefix filter"
        />
      </ConsoleBar>
      <FeedList
        entries={entries}
        label="Log console"
        empty={disconnected ? "Waiting to reconnect…" : "Streaming logs…"}
        render={(entry, i) => <LogRow key={i} entry={entry} />}
      />
    </>
  );
}

/* ── events console ──────────────────────────────────────────────────────── */

/**
 * One recorded module event.
 *
 * The payload is safe to render verbatim: the core rejects credential-shaped keys at emit
 * and redacts again on the way out, so there is nothing to strip here (and the browser is
 * the wrong place to be the last line of that defence anyway).
 */
function EventRow({ entry }: { entry: ModuleEvent }) {
  const [expanded, setExpanded] = useState(false);
  const hasPayload = Object.keys(entry.payload).length > 0;

  return (
    <li className="flex flex-col gap-0.5 py-1 text-ink">
      <div className="flex flex-wrap items-baseline gap-2 font-mono text-[11px]">
        <span className="shrink-0 text-ink-faint">{shortTime(entry.received_at)}</span>
        <Badge tone="dim" className="shrink-0 font-mono">
          {entry.module}
        </Badge>
        <span className="shrink-0 text-accent-strong">{entry.type}</span>
        {entry.entity_ref && (
          <span className="flex-1 break-all text-ink-dim">{entry.entity_ref.title}</span>
        )}
        {hasPayload && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto shrink-0 rounded px-1 text-ink-faint hover:text-ink"
            aria-label={expanded ? "Collapse payload" : "Expand payload"}
          >
            {expanded ? "▲" : "▼"}
          </button>
        )}
      </div>
      {expanded && hasPayload && (
        <pre className="ml-2 overflow-auto rounded-(--radius-field) border border-edge bg-surface-2 p-2 font-mono text-[10px] leading-relaxed text-ink-dim">
          {JSON.stringify(entry.payload, null, 2)}
        </pre>
      )}
    </li>
  );
}

function EventsConsole() {
  const [moduleFilter, setModuleFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");

  const { entries, disconnected, clear } = useSseFeed<ModuleEvent>(
    (signal) => eventStream(moduleFilter || undefined, typeFilter || undefined, signal),
    `${moduleFilter} ${typeFilter}`,
    MAX_DISPLAY,
  );

  return (
    <>
      <ConsoleBar count={entries.length} noun="event" disconnected={disconnected} onClear={clear}>
        <TextInput
          value={moduleFilter}
          onChange={(e) => setModuleFilter(e.target.value)}
          placeholder="Module (e.g. mail)…"
          className="max-w-48 text-xs"
          aria-label="Module filter"
        />
        <TextInput
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          placeholder="Type (e.g. mail.received)…"
          className="max-w-56 text-xs"
          aria-label="Event type filter"
        />
      </ConsoleBar>
      <FeedList
        entries={entries}
        label="Events console"
        empty={disconnected ? "Waiting to reconnect…" : "Waiting for module events…"}
        render={(entry) => <EventRow key={entry.id} entry={entry} />}
      />
    </>
  );
}

/* ── automation runs console (#669) ──────────────────────────────────────── */

function outcomeTone(outcome: string): "dim" | "accent" | "ok" | "warn" | "danger" {
  switch (outcome) {
    case "ok":
      return "ok";
    case "skipped":
      return "warn";
    case "error":
      return "danger";
    default:
      return "dim";
  }
}

/** `1234` prompt + `56` completion tokens → a compact `1234+56 tok`. */
function tokenSummary(run: AutomationRun): string | null {
  if (run.prompt_tokens == null && run.completion_tokens == null) return null;
  return `${run.prompt_tokens ?? "?"}+${run.completion_tokens ?? "?"} tok`;
}

/**
 * One ledger entry: fire → filter verdict → run (model, tokens, duration) → sinks.
 *
 * A skipped run carries its *why* in `error` ("rate cap reached (4/hour)", "runtime
 * paused") and is rendered as loudly as a real one — that visibility is the tab's whole
 * point. The trigger chips are the ADR-0019 hover-cards of the events that caused the
 * run, server-resolved; the automation itself is named via the automations list.
 */
function RunRow({ run, automationName }: { run: AutomationRun; automationName: string }) {
  const [expanded, setExpanded] = useState(false);
  const tokens = tokenSummary(run);

  return (
    <li className="flex flex-col gap-0.5 py-1 text-ink">
      <div className="flex flex-wrap items-baseline gap-2 font-mono text-[11px]">
        <span className="shrink-0 text-ink-faint">{shortTime(run.started_at)}</span>
        <Badge tone="dim" className="shrink-0 font-mono">
          {automationName}
        </Badge>
        <span className="shrink-0 text-ink-dim">{run.filter_verdict}</span>
        <Badge tone={outcomeTone(run.outcome)} className="shrink-0 font-mono uppercase">
          {run.outcome}
        </Badge>
        {run.error && <span className="break-all text-warn">{run.error}</span>}
        {run.model && <span className="shrink-0 text-ink-dim">{run.model}</span>}
        {tokens && <span className="shrink-0 text-ink-faint">{tokens}</span>}
        {run.duration_ms != null && (
          <span className="shrink-0 text-ink-faint">{run.duration_ms} ms</span>
        )}
        {run.sinks_fired.length > 0 && (
          <span className="shrink-0 text-ink-dim">→ {run.sinks_fired.join(", ")}</span>
        )}
        {run.output && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto shrink-0 rounded px-1 text-ink-faint hover:text-ink"
            aria-label={expanded ? "Collapse output" : "Expand output"}
          >
            {expanded ? "▲" : "▼"}
          </button>
        )}
      </div>
      {run.trigger_entity_refs.length > 0 && (
        <span className="flex flex-wrap items-center gap-1 pl-2">
          {run.trigger_entity_refs.map((ref) => (
            <EntityRefChip key={`${ref.module}:${ref.kind}:${ref.ref_id}`} entref={ref} />
          ))}
        </span>
      )}
      {expanded && run.output && (
        <pre className="ml-2 whitespace-pre-wrap rounded-(--radius-field) border border-edge bg-surface-2 p-2 font-mono text-[10px] leading-relaxed text-ink-dim">
          {run.output}
        </pre>
      )}
    </li>
  );
}

function RunsConsole() {
  const [automationFilter, setAutomationFilter] = useState("");
  const [outcomeFilter, setOutcomeFilter] = useState("");
  const [moduleFilter, setModuleFilter] = useState("");
  const [automations, setAutomations] = useState<Automation[]>([]);

  // The automations list names each run's automation and backs the filter dropdowns.
  // Best-effort: without it rows fall back to the raw id and the module filter is empty.
  useEffect(() => {
    let cancelled = false;
    api
      .automations()
      .then((rows) => {
        if (!cancelled) setAutomations(rows);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const { entries, disconnected, clear } = useSseFeed<AutomationRun>(
    (signal) => runStream(automationFilter || undefined, outcomeFilter || undefined, signal),
    `${automationFilter} ${outcomeFilter}`,
    MAX_DISPLAY,
  );

  const byId = new Map(automations.map((a) => [a.id, a]));
  const triggerModules = [
    ...new Set(automations.map((a) => a.event_trigger?.module).filter((m): m is string => !!m)),
  ].sort();

  // The module filter is a client-side view over the automations map (a run itself
  // carries no module), so changing it must not tear down the stream — and a reconnect
  // replay may repeat entries, so the render path de-duplicates on id.
  const seen = new Set<string>();
  const visible = entries.filter((run) => {
    if (seen.has(run.id)) return false;
    seen.add(run.id);
    if (!moduleFilter) return true;
    return byId.get(run.automation_id)?.event_trigger?.module === moduleFilter;
  });

  return (
    <>
      <ConsoleBar count={visible.length} noun="run" disconnected={disconnected} onClear={clear}>
        <Select
          size="sm"
          value={automationFilter}
          onChange={(e) => setAutomationFilter(e.target.value)}
          aria-label="Automation filter"
        >
          <option value="">All automations</option>
          {automations.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </Select>
        <Select
          size="sm"
          value={outcomeFilter}
          onChange={(e) => setOutcomeFilter(e.target.value)}
          aria-label="Outcome filter"
        >
          <option value="">All outcomes</option>
          <option value="ok">ok</option>
          <option value="skipped">skipped</option>
          <option value="error">error</option>
        </Select>
        <Select
          size="sm"
          value={moduleFilter}
          onChange={(e) => setModuleFilter(e.target.value)}
          aria-label="Trigger module filter"
        >
          <option value="">All modules</option>
          {triggerModules.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </Select>
      </ConsoleBar>
      <FeedList
        entries={visible}
        label="Automation runs console"
        empty={disconnected ? "Waiting to reconnect…" : "Waiting for automation runs…"}
        render={(run) => (
          <RunRow
            key={run.id}
            run={run}
            automationName={byId.get(run.automation_id)?.name ?? run.automation_id.slice(0, 8)}
          />
        )}
      />
    </>
  );
}

/* ── main screen ─────────────────────────────────────────────────────────── */

export function ObservabilityScreen() {
  const [tab, setTab] = useState<TabId>("logs");
  const [readiness, setReadiness] = useState<Readiness | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .readiness()
      .then((r) => {
        if (!cancelled) setReadiness(r);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-col gap-3 border-b border-edge p-4">
        <div className="flex items-center justify-between gap-3">
          <h1 className="font-serif text-base text-ink">Observability</h1>
          <Tabs tabs={TABS} value={tab} onChange={setTab} label="Observability views" />
        </div>
        <HealthRow readiness={readiness} />
      </div>

      {/* Mounting exactly one console keeps a hidden tab from holding an SSE
          subscription open — the feeds are live, not cached. */}
      {tab === "logs" && <LogsConsole />}
      {tab === "events" && <EventsConsole />}
      {tab === "runs" && <RunsConsole />}
    </div>
  );
}
