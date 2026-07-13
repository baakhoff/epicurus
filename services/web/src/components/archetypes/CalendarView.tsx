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
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Layers,
  MapPin,
  Repeat,
  SquareCheck,
  Users,
  Video,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { isExternalHref } from "@/components/CardLink";
import type { FormValues } from "@/components/SchemaForm";
import { EmptyState, Spinner, cn, useModalFocus } from "@/components/ui";
import { api } from "@/lib/api";
import { onColor } from "@/lib/color";
import {
  CalendarData,
  type AccountsView,
  type BoardAction,
  type CalendarEvent,
  type CalendarFeedItem,
} from "@/lib/contracts";
import { usePanel } from "@/stores/panel";

import { ActionControl } from "./ActionControl";
import {
  applyDrag,
  DAY_MINUTES,
  eventDayBounds,
  findMoveAction,
  formatHour,
  HOUR_HEIGHT,
  isSameLocalDay,
  layoutDayColumns,
  minutesOfDay,
  pxToSnappedMinutes,
  serializeTimed,
  type DragMode,
} from "./calendarGrid";

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
/** Local floating `YYYY-MM-DD` — never `toISOString()` here, which would UTC-shift a date
 *  near local midnight. Feeds the calendar-feed lexicographic compare (#469) and is the
 *  exact shape SchemaForm's `date_toggle` fields expect (#473). */
const ymd = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

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

/** The toolbar's period label, in both forms the narrow/wide toolbar switches between
 *  via CSS (#562) — `short` drops the parts a ~380px viewport has no room for (the long
 *  month name, the year on a date range), `full` is the unabridged desktop form. */
function periodLabel(view: ViewMode, cursor: Date): { full: string; short: string } {
  if (view === "month") {
    return {
      full: cursor.toLocaleDateString(undefined, { month: "long", year: "numeric" }),
      short: cursor.toLocaleDateString(undefined, { month: "short", year: "numeric" }),
    };
  }
  const { start, end } = visibleRange(view, cursor);
  const range = `${fmtDay(start)} – ${fmtDay(addDays(end, -1))}`;
  return { full: `${range}, ${start.getFullYear()}`, short: range };
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

/** Bucket calendar-feed items (#469, e.g. task due-dates) into local-day lists, keyed the
 *  same way `groupByDay` keys events, so a day cell can look both maps up by one key.
 *  `item.date` is a floating `YYYY-MM-DD` — parsed from its components via the local `Date`
 *  constructor, never `new Date("YYYY-MM-DD")` (which parses as UTC midnight and can read
 *  back as the *previous* local day west of UTC). */
function groupFeedByDay(items: CalendarFeedItem[]): Map<string, CalendarFeedItem[]> {
  const map = new Map<string, CalendarFeedItem[]>();
  for (const item of items) {
    const [y, m, d] = item.date.split("-").map(Number);
    const key = dayKey(new Date(y, m - 1, d));
    const bucket = map.get(key);
    if (bucket) bucket.push(item);
    else map.set(key, [item]);
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

/** A stable, distinct colour per calendar, derived from its id — the fallback when the
 *  provider supplies no colour of its own (#431). */
function calendarColor(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
  return `hsl(${h} 55% 58%)`;
}

/** What the connected-accounts view knows about one calendar token. */
interface CalendarMeta {
  label: string;
  /** The provider's own colour (the user's Google calendar colour), if it supplies one. */
  color: string | null;
  enabled: boolean;
}

/** Index the connected-accounts view by `account[:collection]` token — labels, the
 *  provider's own colours, and which calendars are enabled (#431). */
function buildCalendarMeta(view: AccountsView | undefined): Map<string, CalendarMeta> {
  const meta = new Map<string, CalendarMeta>();
  for (const account of view?.accounts ?? []) {
    for (const col of account.collections) {
      const token = col.collection ? `${col.account}:${col.collection}` : col.account;
      meta.set(token, {
        label: col.title,
        color: col.color ?? null,
        enabled: col.enabled === true,
      });
    }
  }
  return meta;
}

/** Humanised fallback label for a token the accounts view doesn't know. */
function fallbackLabel(id: string): string {
  if (id === "local") return "Local";
  const [account, collection] = id.split(":");
  const acct = account.charAt(0).toUpperCase() + account.slice(1);
  return collection ? `${acct} · ${collection}` : acct;
}

/** One calendar in the visibility menu (#378): every enabled calendar, plus any token an
 *  in-window event carries that the accounts view doesn't list (#431). */
interface CalendarOption {
  id: string;
  label: string;
  color: string;
}

/** Resolves an event's calendar token to its display colour. */
type ColorFor = (id: string | null | undefined) => string;

/* ── view ──────────────────────────────────────────────────────────────────── */

export function CalendarView({ module, pageId }: { module: string; pageId: string }) {
  const [view, setView] = useState<ViewMode>("month");
  const [cursor, setCursor] = useState<Date>(() => new Date());
  const [selected, setSelected] = useState<CalendarEvent | null>(null);
  // Which calendars are hidden (#378), persisted per page so the choice survives a reload.
  const [hidden, setHidden] = useState<Set<string>>(() => readHidden(module, pageId));
  // Clicking an empty day/time slot opens the page's own create-event form, pre-filled
  // (#473) — no new module contract, just seed values fed into the existing action below.
  const [slotSeed, setSlotSeed] = useState<FormValues | null>(null);
  // The day a month-cell tap jumped to (#630) — highlighted in the week view it lands on.
  const [focusedDay, setFocusedDay] = useState<Date | null>(null);
  // Optimistic overlay for a week-grid drag (#631): the new times for an event whose move is
  // in flight, applied over the fetched events so the drag lands instantly and stays put while
  // the write persists — cleared once the refetch confirms it (or rolls back on failure).
  const [pendingMoves, setPendingMoves] = useState<Map<string, { start: Date; end: Date }>>(
    () => new Map(),
  );
  const [moveError, setMoveError] = useState<string | null>(null);
  const queryClient = useQueryClient();

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

  // Calendar names/colours/enabled flags for the menu — best-effort, cached a while; a
  // failure degrades to humanised tokens + derived colours (the existing tests, which mock
  // only modulePage, exercise that path).
  const collections = useQuery({
    queryKey: ["module-collections", module],
    queryFn: () => api.getModuleCollections(module),
    staleTime: 5 * 60_000,
  });
  const meta = useMemo(() => buildCalendarMeta(collections.data), [collections.data]);

  // Task due-dates (and any future module's calendar feed, #469) — a third query merged
  // client-side alongside events, the same precedent `collections` already set. Floating
  // local dates (`ymd`), not `startISO`/`endISO`: the feed endpoint compares `due` (a bare
  // `YYYY-MM-DD`) lexicographically, so a UTC-instant boundary would off-by-one it near
  // local midnight. A down/disabled feed module degrades to "no chips", never blanks events
  // — the core's aggregator already tolerates a 404/unreachable module per-item (ADR-0019).
  const feedStart = ymd(range.start);
  const feedEnd = ymd(range.end);
  const feed = useQuery({
    queryKey: ["calendar-feed", feedStart, feedEnd],
    queryFn: () => api.calendarFeed(feedStart, feedEnd),
    placeholderData: keepPreviousData,
  });
  const feedByDay = useMemo(() => groupFeedByDay(feed.data ?? []), [feed.data]);

  const open = usePanel((s) => s.open);
  // Read-only: clicking a feed chip resolves the owning module's existing hover-card
  // (ADR-0019) and opens it in the right panel — no calendar-specific detail view, no
  // mutation surface (#469 is explicitly read-only; editing a task stays on its own board).
  const openFeedItem = (item: CalendarFeedItem) => {
    api
      .resolveEntity(item.module, item.kind, item.ref_id)
      .then((card) => open("entity-detail", card, item.title))
      .catch(() =>
        open("entity-detail", { title: item.title, description: "", details: [] }, item.title),
      );
  };

  const data = query.data ? CalendarData.parse(query.data) : null;

  // The page's own "New event" action (ADR-0034) — reused as-is for slot-click seeding
  // (#473) rather than inventing a second create surface. Assumes at most one form action;
  // true for calendar today, and a second would just seed whichever is found first.
  const createAction = useMemo(() => (data?.actions ?? []).find((a) => a.form), [data]);

  // The visibility menu lists every *enabled* calendar — not only those with events in
  // the current window (#431) — plus any token an in-window event carries that the
  // accounts view doesn't list (e.g. `local`), so nothing visible is untogglable.
  const calendars = useMemo<CalendarOption[]>(() => {
    const ids: string[] = [];
    for (const [token, m] of meta) if (m.enabled) ids.push(token);
    const extras = new Set<string>();
    for (const ev of data?.events ?? []) {
      if (ev.calendar_id && !ids.includes(ev.calendar_id)) extras.add(ev.calendar_id);
    }
    ids.push(...[...extras].sort());
    return ids.map((id) => ({
      id,
      label: meta.get(id)?.label ?? fallbackLabel(id),
      color: meta.get(id)?.color ?? calendarColor(id),
    }));
  }, [data, meta]);

  // Event chips/rows are tinted with the same colour as their calendar's menu dot (#431):
  // the provider's own colour when it supplies one, else the stable derived hue.
  const colorFor = useMemo<ColorFor>(
    () => (id) => (id ? (meta.get(id)?.color ?? calendarColor(id)) : calendarColor("local")),
    [meta],
  );

  // Apply any in-flight drag (#631) over the fetched events before grouping, so the moved
  // event renders at its new time immediately and the layout (overlap lanes) reflects it.
  const overlaidEvents = useMemo(() => {
    const base = data?.events ?? [];
    if (pendingMoves.size === 0) return base;
    return base.map((ev) => {
      const moved = pendingMoves.get(ev.id);
      return moved ? { ...ev, start: moved.start, end: moved.end } : ev;
    });
  }, [data, pendingMoves]);

  const visibleEvents = useMemo(
    () => overlaidEvents.filter((e) => !(e.calendar_id && hidden.has(e.calendar_id))),
    [overlaidEvents, hidden],
  );
  const byDay = useMemo(() => groupByDay(visibleEvents), [visibleEvents]);

  // Persist a week-grid drag through the event's own editable-calendar action (#208/ADR-0034):
  // find the "Edit" action that can set start/end and invoke it with the new times — the same
  // write the Edit form does, minus the form. Optimistic: the overlay above shows the new time
  // at once; on success we await the refetch (so real data has caught up before the overlay is
  // dropped — no flicker), on failure the overlay is dropped (the event snaps back) and the
  // error is surfaced. The module contract is untouched — the shell just drives it (#631).
  const moveEvent = useCallback(
    async (ev: CalendarEvent, start: Date, end: Date) => {
      const action = findMoveAction(ev);
      if (!action) {
        setMoveError("This event can’t be moved — its calendar is read-only.");
        return;
      }
      setMoveError(null);
      setPendingMoves((prev) => new Map(prev).set(ev.id, { start, end }));
      try {
        await api.invokeModuleTool(module, action.tool, {
          ...action.args,
          start: serializeTimed(start),
          end: serializeTimed(end),
        });
        await queryClient.invalidateQueries({ queryKey: ["module-page", module, pageId] });
      } catch (e) {
        setMoveError(e instanceof Error ? e.message : String(e));
      } finally {
        setPendingMoves((prev) => {
          const next = new Map(prev);
          next.delete(ev.id);
          return next;
        });
      }
    },
    [module, pageId, queryClient],
  );

  // A month-cell tap now navigates into that day's week (#630) instead of starting a create;
  // creation moves to the explicit affordances (the toolbar "New event", and the week grid's
  // empty-slot tap below). The tapped day is remembered so the week view highlights it.
  const openDay = (day: Date) => {
    setCursor(day);
    setFocusedDay(day);
    setView("week");
  };

  // Tapping an empty slot in the week grid seeds the page's own create form (#473) with a
  // timed start at that slot — the same one create surface, now reachable from the grid where
  // Google-style calendars put it. A no-op when the page declares no create action.
  const createSlot = (day: Date, minutes: number) => {
    if (!createAction) return;
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 0, minutes, 0, 0);
    const end = new Date(start.getTime() + 60 * 60_000);
    setSlotSeed({ all_day: false, start: start.toISOString(), end: end.toISOString() });
  };

  const toggleCalendar = (id: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeHidden(module, pageId, next);
      return next;
    });

  return (
    <div className="relative flex h-full min-h-0 flex-col">
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
          <MonthView
            cursor={cursor}
            byDay={byDay}
            feedByDay={feedByDay}
            colorFor={colorFor}
            onSelect={setSelected}
            onOpenFeedItem={openFeedItem}
            onOpenDay={openDay}
          />
        ) : view === "week" ? (
          <WeekView
            cursor={cursor}
            byDay={byDay}
            colorFor={colorFor}
            onSelect={setSelected}
            onMoveEvent={moveEvent}
            focusedDay={focusedDay}
            onCreateSlot={createAction ? createSlot : undefined}
          />
        ) : (
          <AgendaView range={range} byDay={byDay} colorFor={colorFor} onSelect={setSelected} />
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

      {/* A dragged move that the provider rejected (#631): the event has already snapped back
          (its overlay was dropped); this says why, and dismisses itself on the next action. */}
      {moveError && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 z-40 flex justify-center px-4">
          <div
            role="alert"
            className="pointer-events-auto flex max-w-md items-start gap-2 rounded-(--radius-card) border border-danger/40 bg-surface px-3 py-2 text-sm text-ink shadow-(--ep-shadow)"
          >
            <span className="flex-1">{moveError}</span>
            <button
              aria-label="Dismiss"
              onClick={() => setMoveError(null)}
              className="shrink-0 text-ink-faint hover:text-ink"
            >
              <X size={14} />
            </button>
          </div>
        </div>
      )}

      {/* No visible button — a slot click seeds this and opens it directly (#473). Reuses
          the page's own create action/form so there is exactly one create surface. */}
      {createAction && (
        <ActionControl
          module={module}
          pageId={pageId}
          action={createAction}
          initialValues={slotSeed ?? undefined}
          open={slotSeed !== null}
          onOpenChange={(open) => !open && setSlotSeed(null)}
          hideTrigger
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
  /** Both forms of the period label (#562) — `short` renders below `sm`, `full` at/above it. */
  label: { full: string; short: string };
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
        <h2 className="font-serif text-base text-ink">
          <span className="hidden sm:inline">{label.full}</span>
          <span className="sm:hidden">{label.short}</span>
        </h2>
        {fetching && <Spinner className="size-3.5 text-ink-faint" />}
      </div>

      {/* flex-wrap is the last-resort fallback (#562) — icon-only "New event" plus the
          tighter narrow gap should fit this group on one line at ~380px, but a phone
          with several writable calendars (the Calendars menu adds real width) may still
          need the second line rather than clip. */}
      <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
        {/* Page-level actions (e.g. "New event") — core-rendered from the page data (#208).
            size="sm" matches the Today/view-switcher controls in this toolbar (#427);
            iconOnlyNarrow drops the label below `sm`, keeping just the icon (#562). */}
        {actions.map((action) => (
          <ActionControl
            key={action.tool + action.label}
            module={module}
            pageId={pageId}
            action={action}
            size="sm"
            iconOnlyNarrow
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
  feedByDay,
  colorFor,
  onSelect,
  onOpenFeedItem,
  onOpenDay,
}: {
  cursor: Date;
  byDay: Map<string, CalendarEvent[]>;
  /** Task due-dates (and any future module's calendar feed, #469), keyed like `byDay`. */
  feedByDay: Map<string, CalendarFeedItem[]>;
  colorFor: ColorFor;
  onSelect: (ev: CalendarEvent) => void;
  onOpenFeedItem: (item: CalendarFeedItem) => void;
  /** Tapping a day cell opens that day's week view (#630) — creation moved to the toolbar
   *  "New event" and the week grid's empty slots. */
  onOpenDay: (day: Date) => void;
}) {
  const gridStart = startOfWeek(startOfMonth(cursor));
  const days = Array.from({ length: 42 }, (_, i) => addDays(gridStart, i));
  const today = new Date();
  const MAX_CHIPS = 3; // desktop: labelled chips, then "+N more"
  const MAX_FEED_CHIPS = 3;
  const MOBILE_LINES = 10; // phone (#632): slim textless lines, "+N" only past what fits

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
          const feedItems = feedByDay.get(dayKey(day)) ?? [];
          const inMonth = day.getMonth() === cursor.getMonth();
          const today_ = isSameDay(day, today);
          // Phone density (#632): every event + feed item as a slim textless line; detail
          // lives one tap away in the week view now, so the cell trades labels for density.
          const lines = [
            ...evs.map((ev) => ({ key: ev.id, color: colorFor(ev.calendar_id) as string | null })),
            ...feedItems.map((it) => ({ key: `${it.module}:${it.id}`, color: null })),
          ];
          const shownLines = lines.slice(0, MOBILE_LINES);
          const overflowLines = lines.length - shownLines.length;
          return (
            <div
              key={day.toISOString()}
              // Chips / "+more" stopPropagation (they open detail); slim lines bubble — either
              // way a tap on the cell opens that day's week (#630).
              onClick={() => onOpenDay(day)}
              className={cn(
                "flex min-h-0 cursor-pointer flex-col gap-0.5 overflow-hidden border-b border-r border-edge p-1 hover:bg-surface-2/60",
                !inMonth && "bg-surface-2/40",
              )}
            >
              <div className="flex justify-end">
                <span
                  className={cn(
                    "flex size-5 items-center justify-center rounded-full text-xs",
                    today_
                      ? "bg-accent font-medium text-on-accent"
                      : inMonth
                        ? "text-ink-dim"
                        : "text-ink-faint",
                  )}
                >
                  {day.getDate()}
                </span>
              </div>
              {/* Desktop: labelled chips + "+N more". */}
              <div className="hidden min-h-0 flex-col gap-0.5 overflow-hidden sm:flex">
                {evs.slice(0, MAX_CHIPS).map((ev) => (
                  <EventChip key={ev.id} ev={ev} color={colorFor(ev.calendar_id)} onSelect={onSelect} />
                ))}
                {evs.length > MAX_CHIPS && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelect(evs[MAX_CHIPS]);
                    }}
                    className="px-1 text-left text-[11px] text-ink-faint hover:text-ink"
                  >
                    +{evs.length - MAX_CHIPS} more
                  </button>
                )}
                {feedItems.slice(0, MAX_FEED_CHIPS).map((item) => (
                  <FeedItemChip key={`${item.module}:${item.id}`} item={item} onOpen={onOpenFeedItem} />
                ))}
                {feedItems.length > MAX_FEED_CHIPS && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpenFeedItem(feedItems[MAX_FEED_CHIPS]);
                    }}
                    className="px-1 text-left text-[11px] text-ink-faint hover:text-ink"
                  >
                    +{feedItems.length - MAX_FEED_CHIPS} more
                  </button>
                )}
              </div>
              {/* Phone: slim textless lines, one per event/feed item (#632). */}
              <div className="flex min-h-0 flex-col gap-px overflow-hidden sm:hidden">
                {shownLines.map((line) => (
                  <div
                    key={line.key}
                    className={cn("h-1 shrink-0 rounded-full", line.color === null && "bg-ink-faint/40")}
                    style={line.color ? { background: line.color } : undefined}
                  />
                ))}
                {overflowLines > 0 && (
                  <span className="text-[9px] leading-none text-ink-faint">+{overflowLines}</span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** A compact event pill used inside a day cell, tinted with its calendar's colour (#431).
 *  The hovered chip fills with the calendar's own colour — runtime data, so its text
 *  colour is computed per colour (`onColor`, #531) instead of pairing a theme token
 *  with an arbitrary fill (light theme + light calendar used to wash the label out). */
function EventChip({
  ev,
  color,
  onSelect,
}: {
  ev: CalendarEvent;
  color: string;
  onSelect: (ev: CalendarEvent) => void;
}) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation(); // the day cell behind it opens a *create* form on click (#473)
        onSelect(ev);
      }}
      title={ev.title}
      style={{ "--cal": color, "--cal-ink": onColor(color) } as CSSProperties}
      className="flex items-baseline gap-1 truncate rounded-sm bg-[color-mix(in_srgb,var(--cal)_24%,transparent)] px-1 py-0.5 text-left text-[11px] leading-tight text-ink hover:bg-(--cal) hover:text-(--cal-ink)"
    >
      {!ev.all_day && (
        <span className="shrink-0 tabular-nums opacity-80">{fmtTime(ev.start)}</span>
      )}
      <span className="truncate">{ev.title}</span>
    </button>
  );
}

/** A read-only task-due-date marker inside a day cell (#469) — muted and checkbox-glyphed
 *  so it reads as "not an event" at a glance, distinct from a colour-tinted `EventChip`.
 *  No `actions`, no create/edit affordance here: it opens the owning module's own
 *  hover-card in the right panel, the same generic resolve path chat's entity chips use. */
function FeedItemChip({
  item,
  onOpen,
}: {
  item: CalendarFeedItem;
  onOpen: (item: CalendarFeedItem) => void;
}) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation(); // the day cell behind it opens a *create* form on click (#473)
        onOpen(item);
      }}
      title={item.title}
      className="flex items-center gap-1 truncate rounded-sm px-1 py-0.5 text-left text-[11px] leading-tight text-ink-faint hover:bg-surface-2 hover:text-ink-dim"
    >
      <SquareCheck size={11} className="shrink-0" />
      <span className="truncate">{item.title}</span>
    </button>
  );
}

/* ── week (hourly day-grid, #631) ──────────────────────────────────────────── */

const HOURS = Array.from({ length: 24 }, (_, h) => h);

/**
 * The week view as a Google-Calendar-like hourly grid: one column per day, hour rows, timed
 * events placed and sized by start/duration, a pinned all-day strip on top, a current-time
 * line, and drag-to-move / resize that persists through the event's own editable-calendar
 * action (#208/ADR-0034). The whole week is one `overflow-auto` grid — the day headers stay
 * sticky-top, the all-day strip pins below them, and the time gutter stays sticky-left, so on
 * a phone the grid pans horizontally without losing its axes.
 */
function WeekView({
  cursor,
  byDay,
  colorFor,
  onSelect,
  onMoveEvent,
  focusedDay,
  onCreateSlot,
}: {
  cursor: Date;
  byDay: Map<string, CalendarEvent[]>;
  colorFor: ColorFor;
  onSelect: (ev: CalendarEvent) => void;
  /** Persist a dragged event's new times (optimistic + rollback lives in the parent). */
  onMoveEvent: (ev: CalendarEvent, start: Date, end: Date) => void;
  /** The day a month-cell tap jumped to (#630) — its column is highlighted; null otherwise. */
  focusedDay?: Date | null;
  /** Tapping an empty slot seeds the create form at that time (#473, relocated here); omitted
   *  when the page has no create action. */
  onCreateSlot?: (day: Date, minutes: number) => void;
}) {
  const wkStart = startOfWeek(cursor);
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(wkStart, i)), [wkStart]);
  const hasAllDay = days.some((d) => (byDay.get(dayKey(d)) ?? []).some((e) => e.all_day));

  const scrollRef = useRef<HTMLDivElement>(null);
  // A click that concludes a real drag must not also open the event's detail. Set on a moved
  // pointer-up, read (and cleared) by the following click.
  const suppressClickRef = useRef(false);
  // The live drag, kept in a ref so the window pointer handlers read the latest without
  // re-subscribing on every move; `preview`/`dragging` are the render-facing mirror.
  const dragRef = useRef<{
    ev: CalendarEvent;
    mode: DragMode;
    startClientY: number;
    origStart: Date;
    origEnd: Date;
    moved: boolean;
    curStart: Date;
    curEnd: Date;
  } | null>(null);
  const [preview, setPreview] = useState<{ id: string; start: Date; end: Date } | null>(null);
  const [dragging, setDragging] = useState(false);
  const [nowTick, setNowTick] = useState(() => new Date());

  const beginDrag = (ev: CalendarEvent, mode: DragMode, e: ReactPointerEvent) => {
    if (!findMoveAction(ev)) return; // read-only event → not draggable
    if (e.pointerType === "mouse" && e.button !== 0) return; // primary button only
    dragRef.current = {
      ev,
      mode,
      startClientY: e.clientY,
      origStart: ev.start,
      origEnd: ev.end,
      moved: false,
      curStart: ev.start,
      curEnd: ev.end,
    };
    setPreview({ id: ev.id, start: ev.start, end: ev.end });
    setDragging(true);
  };

  // Window-level pointer handlers while a drag is live — one subscription per drag, reading the
  // mutable ref so a fast drag doesn't thrash React subscriptions.
  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: PointerEvent) => {
      const d = dragRef.current;
      if (!d) return;
      const deltaMin = pxToSnappedMinutes(e.clientY - d.startClientY);
      if (deltaMin !== 0) d.moved = true;
      const next = applyDrag(d.origStart, d.origEnd, d.mode, deltaMin);
      d.curStart = next.start;
      d.curEnd = next.end;
      setPreview({ id: d.ev.id, start: next.start, end: next.end });
    };
    const onUp = () => {
      const d = dragRef.current;
      dragRef.current = null;
      // Commit the move (parent applies its own overlay) and drop the preview in the same tick,
      // so the handoff to the optimistic overlay is batched — no snap-back flicker.
      if (d && d.moved) {
        suppressClickRef.current = true;
        onMoveEvent(d.ev, d.curStart, d.curEnd);
      }
      setPreview(null);
      setDragging(false);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [dragging, onMoveEvent]);

  // Tick the current-time line each minute.
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(new Date()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // Open at a sensible scroll position once (morning, or the current time when this week holds
  // today) — mount-only so the operator's scroll survives week-to-week navigation.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const todayInWeek = days.some((d) => isSameLocalDay(d, new Date()));
    const focusMin = todayInWeek ? minutesOfDay(new Date()) : 8 * 60;
    el.scrollTop = Math.max(0, (focusMin / 60) * HOUR_HEIGHT - el.clientHeight / 3);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: position once on mount
  }, []);

  const onEventClick = (ev: CalendarEvent) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    onSelect(ev);
  };

  const nowMin = minutesOfDay(nowTick);

  return (
    <div ref={scrollRef} className="h-full min-h-0 overflow-auto">
      <div
        className="grid"
        style={{ gridTemplateColumns: `3.5rem repeat(7, minmax(6rem, 1fr))` }}
      >
        {/* ── header row: corner + day headers (sticky top) ── */}
        <div className="sticky left-0 top-0 z-40 h-14 border-b border-r border-edge bg-surface" />
        {days.map((day) => {
          const isToday = isSameDay(day, nowTick);
          const isFocused = focusedDay ? isSameDay(day, focusedDay) : false;
          return (
            <div
              key={`h-${dayKey(day)}`}
              className={cn(
                "sticky top-0 z-30 flex h-14 flex-col items-center justify-center border-b border-r border-edge bg-surface last:border-r-0",
                isFocused && !isToday && "bg-accent-dim",
              )}
            >
              <div className="text-[11px] uppercase tracking-wide text-ink-faint">
                {day.toLocaleDateString(undefined, { weekday: "short" })}
              </div>
              <div
                className={cn(
                  "mt-0.5 flex size-6 items-center justify-center rounded-full text-sm",
                  isToday
                    ? "bg-accent font-medium text-on-accent"
                    : isFocused
                      ? "font-medium text-accent-strong ring-1 ring-accent"
                      : "text-ink",
                )}
              >
                {day.getDate()}
              </div>
            </div>
          );
        })}

        {/* ── all-day strip (pinned below the header, ADR-0037) — only when there are any ── */}
        {hasAllDay && (
          <>
            <div className="sticky left-0 top-14 z-30 border-b border-r border-edge bg-surface px-1 py-1 text-right text-[10px] uppercase tracking-wide text-ink-faint">
              All-day
            </div>
            {days.map((day) => {
              const allDay = (byDay.get(dayKey(day)) ?? []).filter((e) => e.all_day);
              return (
                <div
                  key={`ad-${dayKey(day)}`}
                  className="sticky top-14 z-20 flex min-h-[1.75rem] flex-col gap-0.5 border-b border-r border-edge bg-surface p-1 last:border-r-0"
                >
                  {allDay.map((ev) => (
                    <WeekAllDayChip
                      key={ev.id}
                      ev={ev}
                      color={colorFor(ev.calendar_id)}
                      onSelect={onSelect}
                    />
                  ))}
                </div>
              );
            })}
          </>
        )}

        {/* ── body: time gutter (sticky left) + day columns ── */}
        <div className="sticky left-0 z-10 bg-surface">
          {HOURS.map((h) => (
            <div key={h} style={{ height: HOUR_HEIGHT }} className="relative border-r border-edge">
              {h > 0 && (
                <span className="absolute -top-2 right-1 text-[10px] tabular-nums text-ink-faint">
                  {formatHour(h)}
                </span>
              )}
            </div>
          ))}
        </div>
        {days.map((day) => (
          <WeekDayColumn
            key={`c-${dayKey(day)}`}
            day={day}
            events={(byDay.get(dayKey(day)) ?? []).filter((e) => !e.all_day)}
            colorFor={colorFor}
            onEventClick={onEventClick}
            onBeginDrag={beginDrag}
            isToday={isSameDay(day, nowTick)}
            isFocused={focusedDay ? isSameDay(day, focusedDay) : false}
            nowMin={nowMin}
            preview={preview}
            onCreateSlot={onCreateSlot}
          />
        ))}
      </div>
    </div>
  );
}

/** One day's timed column: hour gridlines + absolutely-placed event boxes (lane-packed for
 *  overlaps) + the current-time line when it's today. */
function WeekDayColumn({
  day,
  events,
  colorFor,
  onEventClick,
  onBeginDrag,
  isToday,
  isFocused,
  nowMin,
  preview,
  onCreateSlot,
}: {
  day: Date;
  events: CalendarEvent[];
  colorFor: ColorFor;
  onEventClick: (ev: CalendarEvent) => void;
  onBeginDrag: (ev: CalendarEvent, mode: DragMode, e: ReactPointerEvent) => void;
  isToday: boolean;
  isFocused: boolean;
  nowMin: number;
  preview: { id: string; start: Date; end: Date } | null;
  onCreateSlot?: (day: Date, minutes: number) => void;
}) {
  // Position boxes, mapping the live-dragged event to its preview times so it tracks the pointer.
  const boxes = useMemo(() => {
    const laid = layoutDayColumns(
      events.map((ev) => {
        const t = preview && preview.id === ev.id ? preview : ev;
        const { startMin, endMin } = eventDayBounds({ start: t.start, end: t.end }, day);
        return { id: ev.id, startMin, endMin };
      }),
    );
    return new Map(laid.map((b) => [b.id, b]));
  }, [events, preview, day]);

  // Click empty grid space to create at that half-hour (#473, relocated to the grid). Event
  // boxes stopPropagation, so only genuinely empty space reaches this.
  const onBackgroundClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (!onCreateSlot) return;
    const offsetY = e.clientY - e.currentTarget.getBoundingClientRect().top;
    const raw = (offsetY / HOUR_HEIGHT) * 60;
    const minutes = Math.max(0, Math.min(DAY_MINUTES - 30, Math.floor(raw / 30) * 30));
    onCreateSlot(day, minutes);
  };

  return (
    <div
      onClick={onCreateSlot ? onBackgroundClick : undefined}
      className={cn(
        "relative border-r border-edge last:border-r-0",
        isFocused && !isToday && "bg-accent-dim/25",
        onCreateSlot && "cursor-pointer",
      )}
    >
      {HOURS.map((h) => (
        <div key={h} style={{ height: HOUR_HEIGHT }} className="border-b border-edge/50" />
      ))}
      <div className="absolute inset-0">
        {events.map((ev) => {
          const box = boxes.get(ev.id);
          if (!box) return null;
          return (
            <TimedEventBox
              key={ev.id}
              ev={ev}
              box={box}
              color={colorFor(ev.calendar_id)}
              movable={Boolean(findMoveAction(ev))}
              onBeginDrag={onBeginDrag}
              onClick={onEventClick}
            />
          );
        })}
      </div>
      {isToday && (
        <div
          className="pointer-events-none absolute inset-x-0 z-[2]"
          style={{ top: (nowMin / 60) * HOUR_HEIGHT }}
          aria-hidden
        >
          <div className="-mt-px h-0.5 bg-danger" />
          <div className="absolute -left-0.5 -top-[3px] size-2 rounded-full bg-danger" />
        </div>
      )}
    </div>
  );
}

/** A timed event, absolutely placed in its day column and (when its calendar is writable)
 *  drag-to-move by its body / resize by its bottom edge. */
function TimedEventBox({
  ev,
  box,
  color,
  movable,
  onBeginDrag,
  onClick,
}: {
  ev: CalendarEvent;
  box: { startMin: number; endMin: number; lane: number; lanes: number };
  color: string;
  movable: boolean;
  onBeginDrag: (ev: CalendarEvent, mode: DragMode, e: ReactPointerEvent) => void;
  onClick: (ev: CalendarEvent) => void;
}) {
  const top = (box.startMin / 60) * HOUR_HEIGHT;
  const height = ((box.endMin - box.startMin) / 60) * HOUR_HEIGHT;
  return (
    <button
      type="button"
      onPointerDown={movable ? (e) => onBeginDrag(ev, "move", e) : undefined}
      onClick={(e) => {
        e.stopPropagation(); // the column behind opens a create slot on empty-space clicks
        onClick(ev);
      }}
      title={ev.title}
      style={
        {
          top,
          height,
          left: `calc(${(box.lane / box.lanes) * 100}% + 1px)`,
          width: `calc(${100 / box.lanes}% - 3px)`,
          "--cal": color,
        } as CSSProperties
      }
      className={cn(
        "absolute z-[1] flex flex-col overflow-hidden rounded-sm border border-l-2 px-1 py-0.5 text-left leading-tight select-none",
        "border-[color-mix(in_srgb,var(--cal)_40%,transparent)] border-l-(--cal) bg-[color-mix(in_srgb,var(--cal)_20%,var(--color-surface))]",
        movable ? "cursor-grab touch-none active:cursor-grabbing" : "cursor-pointer",
      )}
    >
      <span className="truncate text-[11px] font-medium text-ink">{ev.title}</span>
      {height >= 32 && (
        <span className="truncate text-[10px] tabular-nums text-ink-dim">{fmtTime(ev.start)}</span>
      )}
      {movable && (
        <span
          onPointerDown={(e) => {
            e.stopPropagation(); // resize, not move
            onBeginDrag(ev, "resize-end", e);
          }}
          className="absolute inset-x-0 bottom-0 h-2 cursor-ns-resize touch-none"
          aria-hidden
        />
      )}
    </button>
  );
}

/** A compact all-day / multi-day chip in the pinned strip, tinted with its calendar's colour. */
function WeekAllDayChip({
  ev,
  color,
  onSelect,
}: {
  ev: CalendarEvent;
  color: string;
  onSelect: (ev: CalendarEvent) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(ev)}
      title={ev.title}
      style={{ "--cal": color } as CSSProperties}
      className="truncate rounded-sm border-l-2 border-(--cal) bg-[color-mix(in_srgb,var(--cal)_18%,var(--color-surface))] px-1 py-0.5 text-left text-[11px] leading-tight text-ink"
    >
      {ev.title}
    </button>
  );
}

/** A taller event card used in the week column and agenda list, edged with its
 *  calendar's colour (#431). */
function EventRow({
  ev,
  color,
  onSelect,
}: {
  ev: CalendarEvent;
  color: string;
  onSelect: (ev: CalendarEvent) => void;
}) {
  return (
    <button
      onClick={() => onSelect(ev)}
      style={{ "--cal": color } as CSSProperties}
      className="flex flex-col gap-0.5 rounded-(--radius-field) border-l-2 border-(--cal) bg-surface-2 px-2 py-1 text-left hover:bg-[color-mix(in_srgb,var(--cal)_16%,transparent)]"
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
  colorFor,
  onSelect,
}: {
  range: { start: Date; end: Date };
  byDay: Map<string, CalendarEvent[]>;
  colorFor: ColorFor;
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
                <EventRow key={ev.id} ev={ev} color={colorFor(ev.calendar_id)} onSelect={onSelect} />
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

const FREQ_LABELS: Record<string, string> = {
  DAILY: "Daily",
  WEEKLY: "Weekly",
  MONTHLY: "Monthly",
  YEARLY: "Yearly",
};

/** A short label for an event's recurrence rule (#432) — mirrors the module's own
 *  `_humanize_recurrence`, so the hover-card and this detail view agree. */
function recurrenceLabel(rule: string): string {
  for (const part of rule.split(";")) {
    const [key, value] = part.split("=");
    if (key === "FREQ" && value) {
      return FREQ_LABELS[value] ?? value[0] + value.slice(1).toLowerCase();
    }
  }
  return "Recurring";
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
  const dialogRef = useRef<HTMLDivElement>(null);
  // The modal focus contract (#512): the same machinery as Sheet/Confirm — focus moves
  // into the dialog on open, Tab wraps inside it, and the chip that opened it regains
  // focus when the overlay unmounts. `open` is literally true: the component only
  // mounts while an event is selected, so mount/unmount are the open/close edges.
  useModalFocus(dialogRef, true);
  // One combined slot for whichever action last failed, rendered below the full
  // actions row rather than per-action inline (#472) — cleared on the next success.
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40" />
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={ev.title}
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-md rounded-(--radius-card) border border-edge bg-surface p-5 outline-none shadow-(--ep-shadow)"
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
        {ev.recurrence && (
          <p className="mt-2 flex items-center gap-1.5 text-sm text-ink-dim">
            <Repeat size={14} className="shrink-0" />
            {recurrenceLabel(ev.recurrence)}
          </p>
        )}
        {ev.attendees.length > 0 && (
          <p className="mt-2 flex items-start gap-1.5 text-sm text-ink-dim">
            <Users size={14} className="mt-0.5 shrink-0" />
            <span>{ev.attendees.map((a) => a.display_name ?? a.email).join(", ")}</span>
          </p>
        )}
        {/* provider-supplied URL — same trust rule as CardLink: http(s) only */}
        {ev.meet_url && isExternalHref(ev.meet_url) && (
          <p className="mt-2 flex items-center gap-1.5 text-sm">
            <Video size={14} className="shrink-0 text-ink-dim" />
            <a
              href={ev.meet_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent hover:underline"
              onClick={(e) => e.stopPropagation()}
            >
              Join with Google Meet
            </a>
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
                onError={setActionError}
              />
            ))}
          </div>
        )}
        {actionError && <p className="mt-2 text-[11px] text-danger">{actionError}</p>}
      </div>
    </div>
  );
}
