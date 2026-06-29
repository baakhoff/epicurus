/**
 * The Observability screen — a live log console + system health summary.
 *
 * The operator can watch what core-app is doing in real time without ``docker logs``.
 * The page owns its own SSE connection (reconnects on disconnect) and keeps at most
 * MAX_DISPLAY entries in local state so the DOM stays snappy.
 */
import { Check, WifiOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { Badge, Button, Select, Spinner, TextInput, cn } from "@/components/ui";
import { api, logStream } from "@/lib/api";
import type { LogEntry, Readiness, ReadinessComponent } from "@/lib/contracts";

/* ── constants ───────────────────────────────────────────────────────────── */

const MAX_DISPLAY = 500;

type LevelFilter = "debug" | "info" | "warning" | "error";
const LEVEL_OPTIONS: { value: LevelFilter; label: string }[] = [
  { value: "debug", label: "All" },
  { value: "info", label: "Info+" },
  { value: "warning", label: "Warning+" },
  { value: "error", label: "Error only" },
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

/* ── log row ─────────────────────────────────────────────────────────────── */

function LogRow({ entry }: { entry: LogEntry }) {
  // Format the ISO timestamp to a readable short form.
  const time = (() => {
    try {
      return new Date(entry.ts).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        fractionalSecondDigits: 3,
        hour12: false,
      });
    } catch {
      return entry.ts;
    }
  })();

  const hasContext = Object.keys(entry.context).length > 0;
  const [expanded, setExpanded] = useState(false);

  return (
    <li className={cn("flex flex-col gap-0.5 py-1", levelTextClass(entry.level))}>
      <div className="flex flex-wrap items-baseline gap-2 font-mono text-[11px]">
        <span className="shrink-0 text-ink-faint">{time}</span>
        <Badge tone={levelBadgeTone(entry.level)} className="shrink-0 font-mono uppercase">
          {entry.level === "critical" ? "CRIT" : entry.level.toUpperCase().slice(0, 4)}
        </Badge>
        {entry.service && (
          <span className="shrink-0 text-ink-dim">{entry.service}</span>
        )}
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

/* ── main screen ─────────────────────────────────────────────────────────── */

export function ObservabilityScreen() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [levelFilter, setLevelFilter] = useState<LevelFilter>("info");
  const [serviceFilter, setServiceFilter] = useState("");
  const [disconnected, setDisconnected] = useState(false);
  const [readiness, setReadiness] = useState<Readiness | null>(null);

  // Clear the visible entries when a filter changes — adjust state during render
  // (the React-blessed alternative to a setState-in-effect; mirrors EditorView).
  const filterKey = `${levelFilter} ${serviceFilter}`;
  const [appliedFilterKey, setAppliedFilterKey] = useState(filterKey);
  if (filterKey !== appliedFilterKey) {
    setAppliedFilterKey(filterKey);
    setEntries([]);
  }

  const bottomRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Fetch readiness once on mount.
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

  // Auto-scroll: only scroll to bottom when the user is already near the bottom.
  const shouldAutoScroll = useCallback(() => {
    const list = listRef.current;
    if (!list) return true;
    const threshold = 120; // px from bottom
    return list.scrollHeight - list.scrollTop - list.clientHeight < threshold;
  }, []);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  // SSE streaming with auto-reconnect on disconnect.
  useEffect(() => {
    let active = true;

    async function connect() {
      while (active) {
        abortRef.current?.abort();
        const abort = new AbortController();
        abortRef.current = abort;
        setDisconnected(false);
        try {
          for await (const entry of logStream(levelFilter, serviceFilter || undefined, abort.signal)) {
            if (!active) break;
            setEntries((prev) => {
              const next = [...prev, entry];
              return next.length > MAX_DISPLAY ? next.slice(next.length - MAX_DISPLAY) : next;
            });
          }
          // Stream ended cleanly (server closed); reconnect after a short delay.
          if (active) {
            setDisconnected(true);
            await delay(3_000);
          }
        } catch (err: unknown) {
          if (!active) break;
          // AbortError means we intentionally killed the connection (filter change / unmount).
          if (err instanceof Error && err.name === "AbortError") break;
          setDisconnected(true);
          await delay(3_000);
        }
      }
    }

    connect().catch(() => {});

    return () => {
      active = false;
      abortRef.current?.abort();
    };
  }, [levelFilter, serviceFilter]);

  // Scroll to bottom when new entries arrive (if near bottom).
  useEffect(() => {
    if (shouldAutoScroll()) scrollToBottom();
  }, [entries, shouldAutoScroll, scrollToBottom]);

  const handleClear = () => setEntries([]);

  return (
    <div className="flex h-full flex-col">
      {/* top bar */}
      <div className="flex flex-col gap-3 border-b border-edge p-4">
        <div className="flex items-center justify-between">
          <h1 className="font-serif text-base text-ink">Observability</h1>
          <div className="flex items-center gap-2">
            {disconnected && (
              <span className="flex items-center gap-1 text-xs text-warn">
                <WifiOff size={12} />
                Reconnecting…
              </span>
            )}
            <Button variant="ghost" onClick={handleClear} className="text-xs">
              Clear
            </Button>
          </div>
        </div>

        {/* health summary */}
        <HealthRow readiness={readiness} />

        {/* filters */}
        <div className="flex flex-wrap items-center gap-2">
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

          <span className="ml-auto text-xs text-ink-faint">
            {entries.length} {entries.length === 1 ? "entry" : "entries"}
          </span>
        </div>
      </div>

      {/* log console */}
      <ul
        ref={listRef}
        className="min-h-0 flex-1 overflow-y-auto divide-y divide-edge px-4 py-2"
        aria-label="Log console"
        aria-live="polite"
        aria-atomic={false}
      >
        {entries.length === 0 ? (
          <li className="flex items-center justify-center py-10 text-sm text-ink-faint">
            {disconnected ? "Waiting to reconnect…" : "Streaming logs…"}
          </li>
        ) : (
          entries.map((entry, i) => <LogRow key={i} entry={entry} />)
        )}
        <div ref={bottomRef} />
      </ul>
    </div>
  );
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
