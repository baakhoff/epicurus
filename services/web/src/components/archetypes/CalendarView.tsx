/**
 * The `calendar` archetype (ADR-0018): month / week / agenda views, core-rendered.
 * The module supplies only data — events within a `[start, end)` window it never
 * chooses — through the core page proxy; this screen owns all chrome and styling.
 * No module markup runs here.
 *
 * Navigation re-fetches the visible window (the core forwards `start`/`end` to the
 * module), so the calendar scrolls arbitrarily far without loading every event up
 * front. Times are read in the viewer's local zone, as a calendar should be.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Layers, MapPin, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { EmptyState, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import {
  CalendarData,
  type AccountsView,
  type BoardAction,
  type CalendarEvent,
} from "@/lib/contracts";

import { ActionControl } from "./ActionControl";

type ViewMode = "month" | "week" | "agenda";

const VIEWS: { id: ViewMode; label: string }[] = [
  { id: "month", label: "Month" },
  { id: "week", label: "Week" },
  { id: "agenda", label: "Agenda" },
];

/* ── date helpers (local time — weeks start Monday, per ISO-8601) ──────────── */

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const addMonths = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth() + n, 1);
const startOfMonth = (d: Date) => new Date(d.getFullYear(), d.getMonth(), 1);
const startOfWeek = (d: Date) => addDays(startOfDay(d), -((d.getDay() + 6) % 7));
const isSameDay = (a: Date, b: Date) => startOfDay(a).getTime() === startOfDay(b).getTime();
const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const AGENDA_DAYS = 28;

/** The `[start, end)` window a view shows, as local Date bounds. */
function visibleRange(view: ViewMode, cursor: Date): { start: Date; end: Date } {
  if (view === "month") {
    const gridStart = startOfWeek(startOfMonth(cursor));
    return { start: gridStart, end: addDays(gridStart, 42) }; // a fixed 6-week grid
  }
  if (view === "week") {
    const wkStart = startOfWeek(cursor);
    return { start: wkStart, end: addDays(wkStart, 7) };
  }
  const aStart = startOfDay(cursor);
  return { start: aStart, end: addDays(aStart, AGENDA_DAYS) };
}

/** Step the cursor by one unit of the active view. */
function step(view: ViewMode, cursor: Date, dir: 1 | -1): Date {
  if (view === "month") return addMonths(cursor, dir);
  if (view === "week") return addDays(cursor, 7 * dir);
  return addDays(cursor, AGENDA_DAYS * dir);
}

const fmtDay = (d: Date) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
const fmtTime = (d: Date) => d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

function periodLabel(view: ViewMode, cursor: Date): string {
  if (view === "month") {
    return cursor.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  }
  const { start, end } = visibleRange(view, cursor);
  return `${fmtDay(start)} – ${fmtDay(addDays(end, -1))}, ${start.getFullYear()}`;
}

/** Bucket events into local-day lists, each ordered by start time.
 *
 * An all-day event is placed on every day in its `[start, end)` span (end exclusive) so a
 * multi-day all-day event (a trip, holidays) shows on each day; timed events sit on their
 * start day. All-day starts at local midnight, so they sort first within a day. */
function groupByDay(events: CalendarEvent[]): Map<string, CalendarEvent[]> {
  const map = new Map<string, CalendarEvent[]>();
  const push = (key: string, ev: CalendarEvent) => {
    const bucket = map.get(key);
    if (bucket) bucket.push(ev);
    else map.set(key, [ev]);
  };
  for (const ev of [...events].sort((a, b) => a.start.getTime() - b.start.getTime())) {
    if (ev.all_day) {
      let day = startOfDay(ev.start);
      if (ev.end.getTime() <= day.getTime()) {
        push(dayKey(day), ev); // degenerate span — at least show it once
        continue;
      }
      for (; day.getTime() < ev.end.getTime(); day = addDays(day, 1)) push(dayKey(day), ev);
    } else {
      push(dayKey(ev.start), ev);
    }
  }
  return map;
}

/* ── per-month cache (#379): paint the cached window instantly, revalidate in the background ── */

const CACHE_KEY = "epicurus-cal-cache";
const CACHE_WINDOWS = 12; // keep the last N windows — a disposable cache (constraint #2)

interface CachedWindow {
  at: number;
  data: unknown;
}

const rangeCacheKey = (module: string, pageId: string, startISO: string, endISO: string): string =>
  `${module}:${pageId}:${startISO}:${endISO}`;

function readCache(): Record<string, CachedWindow> {
  try {
    return JSON.parse(localStorage.getItem(CACHE_KEY) ?? "{}") as Record<string, CachedWindow>;
  } catch {
    return {};
  }
}

function readWindow(key: string): unknown {
  return readCache()[key]?.data;
}

function writeWindow(key: string, data: unknown): void {
  try {
    const all = readCache();
    all[key] = { at: Date.now(), data };
    // Bound the cache: keep only the most-recently-written windows.
    const kept: Record<string, CachedWindow> = {};
    for (const k of Object.keys(all)
      .sort((a, b) => all[b].at - all[a].at)
      .slice(0, CACHE_WINDOWS)) {
      kept[k] = all[k];
    }
    localStorage.setItem(CACHE_KEY, JSON.stringify(kept));
  } catch {
    /* storage full / unavailable — caching is best-effort */
  }
}

/* ── visible-calendar selection (#378), persisted per page ───────────────────── */

const hiddenStorageKey = (module: string, pageId: string): string =>
  `epicurus-cal-hidden:${module}:${pageId}`;

function readHidden(module: string, pageId: string): Set<string> {
  try {
    const raw = localStorage.getItem(hiddenStorageKey(module, pageId));
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

function writeHidden(module: string, pageId: string, hidden: Set<string>): void {
  try {
    localStorage.setItem(hiddenStorageKey(module, pageId), JSON.stringify([...hidden]));
  } catch {
    /* best-effort */
  }
}

/** A stable, distinct colour per calendar, derived from its id (no backend colour needed). */
function calendarColor(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
  return `hsl(${h} 55% 58%)`;
}

/** Map a calendar `account[:collection]` token to a human label — from the connected-accounts
 *  view when available, else a humanised token, so a missing/disconnected list still reads. */
function buildLabelMap(view: AccountsView | undefined): (id: string) => string {
  const titles = new Map<string, string>();
  for (const account of view?.accounts ?? []) {
    for (const col of account.collections) {
      const token = col.collection ? `${col.account}:${col.collection}` : col.account;
      titles.set(token, col.title);
    }
  }
  return (id: string) => {
    const title = titles.get(id);
    if (title) return title;
    if (id === "local") return "Local";
    const [account, collection] = id.split(":");
    const acct = account.charAt(0).toUpperCase() + account.slice(1);
    return collection ? `${acct} · ${collection}` : acct;
  };
}

/** A calendar contributing events to the current window, for the visibility toggles (#378). */
interface CalendarOption {
  id: string;
  label: string;
  color: string;
}

/* ── view ──────────────────────────────────────────────────────────────────── */

export function CalendarView({ module, pageId }: { module: string; pageId: string }) {
  const [view, setView] = useState<ViewMode>("month");
  const [cursor, setCursor] = useState<Date>(() => new Date());
  const [selected, setSelected] = useState<CalendarEvent | null>(null);
  // Which calendars are hidden (#378), persisted per page so the choice survives a reload.
  const [hidden, setHidden] = useState<Set<string>>(() => readHidden(module, pageId));

  const range = useMemo(() => visibleRange(view, cursor), [view, cursor]);
  const startISO = range.start.toISOString();
  const endISO = range.end.toISOString();
  const cacheKey = rangeCacheKey(module, pageId, startISO, endISO);

  const query = useQuery({
    queryKey: ["module-page", module, pageId, startISO, endISO],
    queryFn: () => api.modulePage(module, pageId, { start: startISO, end: endISO }),
    placeholderData: keepPreviousData,
    // #379: seed from the persisted cache so the window paints instantly on open, then
    // revalidate — `initialDataUpdatedAt: 0` marks the seed stale so a refetch fires at once.
    initialData: () => readWindow(cacheKey),
    initialDataUpdatedAt: 0,
  });

  // Persist each freshly-fetched window so the next open of this range paints from cache (#379).
  useEffect(() => {
    if (query.data && !query.isPlaceholderData) writeWindow(cacheKey, query.data);
  }, [query.data, query.isPlaceholderData, cacheKey]);

  // Calendar names for the toggle labels — best-effort, cached a while; a failure degrades to
  // humanised tokens (the existing tests, which mock only modulePage, exercise that path).
  const collections = useQuery({
    queryKey: ["module-collections", module],
    queryFn: () => api.getModuleCollections(module),
    staleTime: 5 * 60_000,
  });
  const labelFor = useMemo(() => buildLabelMap(collections.data), [collections.data]);

  const data = query.data ? CalendarData.parse(query.data) : null;

  // The calendars contributing events to this window — the visibility toggles (#378).
  const calendars = useMemo<CalendarOption[]>(() => {
    const ids = new Set<string>();
    for (const ev of data?.events ?? []) if (ev.calendar_id) ids.add(ev.calendar_id);
    return [...ids].sort().map((id) => ({ id, label: labelFor(id), color: calendarColor(id) }));
  }, [data, labelFor]);

  const visibleEvents = useMemo(
    () => (data?.events ?? []).filter((e) => !(e.calendar_id && hidden.has(e.calendar_id))),
    [data, hidden],
  );
  const byDay = useMemo(() => groupByDay(visibleEvents), [visibleEvents]);

  const toggleCalendar = (id: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeHidden(module, pageId, next);
      return next;
    });

  return (
    <div className="flex h-full min-h-0 flex-col">
      <Toolbar
        view={view}
        label={periodLabel(view, cursor)}
        fetching={query.isFetching}
        actions={data?.actions ?? []}
        module={module}
        pageId={pageId}
        calendars={calendars}
        hidden={hidden}
        onToggleCalendar={toggleCalendar}
        onView={setView}
        onPrev={() => setCursor((c) => step(view, c, -1))}
        onNext={() => setCursor((c) => step(view, c, 1))}
        onToday={() => setCursor(new Date())}
      />

      <div className="min-h-0 flex-1">
        {!data && query.isLoading ? (
          <div className="flex h-full items-center justify-center">
            <Spinner />
          </div>
        ) : query.isError ? (
          <div className="flex h-full items-center justify-center p-6">
            <EmptyState quote="The calendar is resting.">
              <p className="text-sm text-ink-dim">{(query.error as Error).message}</p>
            </EmptyState>
          </div>
        ) : view === "month" ? (
          <MonthView cursor={cursor} byDay={byDay} onSelect={setSelected} />
        ) : view === "week" ? (
          <WeekView cursor={cursor} byDay={byDay} onSelect={setSelected} />
        ) : (
          <AgendaView range={range} byDay={byDay} onSelect={setSelected} />
        )}
      </div>

      {selected && (
        <EventDetail
          ev={selected}
          module={module}
          pageId={pageId}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

/* ── toolbar ─────────────────────────────────────────────────────────────── */

function Toolbar({
  view,
  label,
  fetching,
  actions,
  module,
  pageId,
  calendars,
  hidden,
  onToggleCalendar,
  onView,
  onPrev,
  onNext,
  onToday,
}: {
  view: ViewMode;
  label: string;
  fetching: boolean;
  actions: BoardAction[];
  module: string;
  pageId: string;
  calendars: CalendarOption[];
  hidden: Set<string>;
  onToggleCalendar: (id: string) => void;
  onView: (v: ViewMode) => void;
  onPrev: () => void;
  onNext: () => void;
  onToday: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-3 py-2">
      <div className="flex items-center gap-2">
        <div className="flex items-center">
          <button
            aria-label="Previous"
            onClick={onPrev}
            className="rounded-(--radius-field) p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
          >
            <ChevronLeft size={18} />
          </button>
          <button
            aria-label="Next"
            onClick={onNext}
            className="rounded-(--radius-field) p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
          >
            <ChevronRight size={18} />
          </button>
        </div>
        <h2 className="font-serif text-base text-ink">{label}</h2>
        {fetching && <Spinner className="size-3.5 text-ink-faint" />}
      </div>

      <div className="flex items-center gap-2">
        {/* Page-level actions (e.g. "New event") — core-rendered from the page data (#208).
            size="sm" matches the Today/view-switcher controls in this toolbar (#427). */}
        {actions.map((action) => (
          <ActionControl
            key={action.tool + action.label}
            module={module}
            pageId={pageId}
            action={action}
            size="sm"
          />
        ))}
        {/* Pick which calendars are visible (#378) — only when more than one is in view. */}
        {calendars.length >= 2 && (
          <CalendarsMenu calendars={calendars} hidden={hidden} onToggle={onToggleCalendar} />
        )}
        <button
          onClick={onToday}
          className="rounded-(--radius-field) border border-edge-strong px-2.5 py-1 text-xs text-ink-dim hover:border-accent hover:text-accent-strong"
        >
          Today
        </button>
        <div className="flex rounded-(--radius-field) border border-edge p-0.5">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              onClick={() => onView(v.id)}
              className={cn(
                "rounded-[calc(var(--radius-field)-2px)] px-2.5 py-1 text-xs transition-colors",
                v.id === view
                  ? "bg-accent-dim text-accent-strong"
                  : "text-ink-dim hover:text-ink",
              )}
            >
              {v.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ── calendar visibility menu (#378) ─────────────────────────────────────── */

/**
 * A dropdown of per-calendar visibility toggles. Each row shows the calendar's colour dot and
 * name; clicking toggles it (hidden ones are struck through with a dimmed dot). The selection
 * is owned + persisted by {@link CalendarView}; this is purely presentational.
 */
function CalendarsMenu({
  calendars,
  hidden,
  onToggle,
}: {
  calendars: CalendarOption[];
  hidden: Set<string>;
  onToggle: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const shown = calendars.length - calendars.filter((c) => hidden.has(c.id)).length;
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-label="Choose visible calendars"
        aria-expanded={open}
        className={cn(
          "flex items-center gap-1.5 rounded-(--radius-field) border border-edge-strong px-2.5 py-1 text-xs transition-colors hover:border-accent hover:text-accent-strong",
          shown < calendars.length ? "text-accent-strong" : "text-ink-dim",
        )}
      >
        <Layers size={14} />
        <span>Calendars{shown < calendars.length ? ` (${shown}/${calendars.length})` : ""}</span>
      </button>
      {open && (
        <>
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            className="fixed inset-0 z-10 cursor-default"
            onClick={() => setOpen(false)}
          />
          <div className="absolute right-0 top-full z-20 mt-1 w-56 overflow-hidden rounded-(--radius-card) border border-edge bg-surface py-1 shadow-(--ep-shadow)">
            {calendars.map((c) => {
              const visible = !hidden.has(c.id);
              return (
                <button
                  key={c.id}
                  onClick={() => onToggle(c.id)}
                  role="switch"
                  aria-checked={visible}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-surface-2"
                >
                  <span
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ background: c.color, opacity: visible ? 1 : 0.3 }}
                  />
                  <span
                    className={cn(
                      "min-w-0 flex-1 truncate",
                      visible ? "text-ink" : "text-ink-faint line-through",
                    )}
                  >
                    {c.label}
                  </span>
                  {visible && <Check size={14} className="shrink-0 text-accent" />}
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

/* ── month ───────────────────────────────────────────────────────────────── */

function MonthView({
  cursor,
  byDay,
  onSelect,
}: {
  cursor: Date;
  byDay: Map<string, CalendarEvent[]>;
  onSelect: (ev: CalendarEvent) => void;
}) {
  const gridStart = startOfWeek(startOfMonth(cursor));
  const days = Array.from({ length: 42 }, (_, i) => addDays(gridStart, i));
  const today = new Date();
  const MAX_CHIPS = 3;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="grid grid-cols-7 border-b border-edge">
        {WEEKDAYS.map((w) => (
          <div key={w} className="px-2 py-1.5 text-center text-xs font-medium text-ink-faint">
            {w}
          </div>
        ))}
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-7 grid-rows-6">
        {days.map((day) => {
          const evs = byDay.get(dayKey(day)) ?? [];
          const inMonth = day.getMonth() === cursor.getMonth();
          const today_ = isSameDay(day, today);
          return (
            <div
              key={day.toISOString()}
              className={cn(
                "flex min-h-0 flex-col gap-0.5 overflow-hidden border-b border-r border-edge p-1",
                !inMonth && "bg-surface-2/40",
              )}
            >
              <div className="flex justify-end">
                <span
                  className={cn(
                    "flex size-5 items-center justify-center rounded-full text-xs",
                    today_
                      ? "bg-accent font-medium text-canvas"
                      : inMonth
                        ? "text-ink-dim"
                        : "text-ink-faint",
                  )}
                >
                  {day.getDate()}
                </span>
              </div>
              <div className="flex min-h-0 flex-col gap-0.5 overflow-hidden">
                {evs.slice(0, MAX_CHIPS).map((ev) => (
                  <EventChip key={ev.id} ev={ev} onSelect={onSelect} />
                ))}
                {evs.length > MAX_CHIPS && (
                  <button
                    onClick={() => onSelect(evs[MAX_CHIPS])}
                    className="px-1 text-left text-[11px] text-ink-faint hover:text-ink"
                  >
                    +{evs.length - MAX_CHIPS} more
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** A compact event pill used inside a day cell. */
function EventChip({ ev, onSelect }: { ev: CalendarEvent; onSelect: (ev: CalendarEvent) => void }) {
  return (
    <button
      onClick={() => onSelect(ev)}
      title={ev.title}
      className="flex items-baseline gap-1 truncate rounded-sm bg-accent-dim px-1 py-0.5 text-left text-[11px] leading-tight text-accent-strong hover:bg-accent hover:text-canvas"
    >
      {!ev.all_day && (
        <span className="shrink-0 tabular-nums opacity-80">{fmtTime(ev.start)}</span>
      )}
      <span className="truncate">{ev.title}</span>
    </button>
  );
}

/* ── week ────────────────────────────────────────────────────────────────── */

function WeekView({
  cursor,
  byDay,
  onSelect,
}: {
  cursor: Date;
  byDay: Map<string, CalendarEvent[]>;
  onSelect: (ev: CalendarEvent) => void;
}) {
  const wkStart = startOfWeek(cursor);
  const days = Array.from({ length: 7 }, (_, i) => addDays(wkStart, i));
  const today = new Date();

  return (
    <div className="flex h-full min-h-0 overflow-x-auto">
      {days.map((day) => {
        const evs = byDay.get(dayKey(day)) ?? [];
        const today_ = isSameDay(day, today);
        return (
          <div
            key={day.toISOString()}
            className="flex min-w-[8.5rem] flex-1 flex-col border-r border-edge last:border-r-0"
          >
            <div className="sticky top-0 border-b border-edge bg-surface px-2 py-1.5 text-center">
              <div className="text-[11px] uppercase tracking-wide text-ink-faint">
                {day.toLocaleDateString(undefined, { weekday: "short" })}
              </div>
              <div
                className={cn(
                  "mx-auto mt-0.5 flex size-6 items-center justify-center rounded-full text-sm",
                  today_ ? "bg-accent font-medium text-canvas" : "text-ink",
                )}
              >
                {day.getDate()}
              </div>
            </div>
            <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-1.5">
              {evs.length === 0 ? (
                <span className="px-1 pt-1 text-[11px] text-ink-faint">—</span>
              ) : (
                evs.map((ev) => <EventRow key={ev.id} ev={ev} onSelect={onSelect} />)
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** A taller event card used in the week column and agenda list. */
function EventRow({ ev, onSelect }: { ev: CalendarEvent; onSelect: (ev: CalendarEvent) => void }) {
  return (
    <button
      onClick={() => onSelect(ev)}
      className="flex flex-col gap-0.5 rounded-(--radius-field) border-l-2 border-accent bg-surface-2 px-2 py-1 text-left hover:bg-accent-dim"
    >
      <span className="truncate text-xs font-medium text-ink">{ev.title}</span>
      <span className="text-[11px] tabular-nums text-ink-dim">
        {ev.all_day ? "All day" : `${fmtTime(ev.start)} – ${fmtTime(ev.end)}`}
      </span>
    </button>
  );
}

/* ── agenda ──────────────────────────────────────────────────────────────── */

function AgendaView({
  range,
  byDay,
  onSelect,
}: {
  range: { start: Date; end: Date };
  byDay: Map<string, CalendarEvent[]>;
  onSelect: (ev: CalendarEvent) => void;
}) {
  const today = new Date();
  // Only days that actually carry events, in chronological order.
  const days: Date[] = [];
  for (let d = startOfDay(range.start); d < range.end; d = addDays(d, 1)) {
    if ((byDay.get(dayKey(d)) ?? []).length > 0) days.push(d);
  }

  if (days.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState quote="Nothing scheduled in this stretch." />
      </div>
    );
  }

  return (
    <div className="mx-auto h-full max-w-2xl overflow-y-auto px-4 py-3">
      <ul className="flex flex-col gap-4">
        {days.map((day) => (
          <li key={day.toISOString()} className="grid grid-cols-[4.5rem_1fr] gap-3">
            <div className="pt-1 text-right">
              <div
                className={cn(
                  "text-sm font-medium",
                  isSameDay(day, today) ? "text-accent-strong" : "text-ink",
                )}
              >
                {day.toLocaleDateString(undefined, { day: "numeric", month: "short" })}
              </div>
              <div className="text-[11px] text-ink-faint">
                {day.toLocaleDateString(undefined, { weekday: "short" })}
              </div>
            </div>
            <ul className="flex flex-col gap-1.5 border-l border-edge pl-3">
              {(byDay.get(dayKey(day)) ?? []).map((ev) => (
                <EventRow key={ev.id} ev={ev} onSelect={onSelect} />
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ── event detail ────────────────────────────────────────────────────────── */

/** A human-readable date+time line for the detail modal. */
function whenLabel(ev: CalendarEvent): string {
  const dayFmt: Intl.DateTimeFormatOptions = { weekday: "long", month: "long", day: "numeric" };
  const startDay = ev.start.toLocaleDateString(undefined, dayFmt);
  if (ev.all_day) {
    const lastDay = addDays(ev.end, -1); // exclusive end → inclusive last day
    if (isSameDay(ev.start, lastDay)) return `${startDay} · All day`;
    return `${startDay} → ${lastDay.toLocaleDateString(undefined, dayFmt)} · All day`;
  }
  if (isSameDay(ev.start, ev.end)) {
    return `${startDay} · ${fmtTime(ev.start)} – ${fmtTime(ev.end)}`;
  }
  return `${startDay} ${fmtTime(ev.start)} → ${ev.end.toLocaleDateString(undefined, dayFmt)} ${fmtTime(ev.end)}`;
}

function EventDetail({
  ev,
  module,
  pageId,
  onClose,
}: {
  ev: CalendarEvent;
  module: string;
  pageId: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={ev.title}
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-md rounded-(--radius-card) border border-edge bg-surface p-5 shadow-(--ep-shadow)"
      >
        <button
          aria-label="Close"
          onClick={onClose}
          className="absolute right-3 top-3 text-ink-faint hover:text-ink"
        >
          <X size={16} />
        </button>
        <h2 className="pr-6 font-serif text-lg text-ink">{ev.title}</h2>
        <p className="mt-1 text-sm text-ink-dim">{whenLabel(ev)}</p>
        {ev.location && (
          <p className="mt-2 flex items-center gap-1.5 text-sm text-ink-dim">
            <MapPin size={14} className="shrink-0" />
            {ev.location}
          </p>
        )}
        {ev.description && (
          <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-ink">
            {ev.description}
          </p>
        )}
        {ev.provider && <p className="mt-4 text-xs text-ink-faint">via {ev.provider}</p>}
        {ev.actions.length > 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-edge pt-3">
            {ev.actions.map((action) => (
              <ActionControl
                key={action.tool + action.label}
                module={module}
                pageId={pageId}
                action={action}
                compact
                onSuccess={onClose}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
