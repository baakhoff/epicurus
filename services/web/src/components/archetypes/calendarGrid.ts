/**
 * Pure geometry + interaction math for the `calendar` week grid (#631) — framework-free so
 * the fiddly parts (overlap lane packing, drag snapping, wall-clock-preserving shifts) get
 * fast, deterministic unit coverage without a DOM. The React grid in {@link CalendarView}
 * imports these; it owns only the rendering and pointer wiring.
 *
 * The module supplies data and the shell renders (ADR-0018/0023) — none of this touches the
 * module contract. A drag persists through the event's *existing* editable-calendar action
 * (#208 / ADR-0034): {@link findMoveAction} finds the "Edit" form action that can set
 * `start`/`end`, and the grid invokes it with new times — exactly what the Edit form submits,
 * minus the form.
 */
import type { BoardAction, CalendarEvent } from "@/lib/contracts";

/** Pixels per hour row in the scrollable grid — the one knob that sets the grid's density. */
export const HOUR_HEIGHT = 48;
/** Minutes the drag snaps to (quarter-hour, like Google's week grid). */
export const SNAP_MIN = 15;
/** Shortest event we still draw at full grab-able height, in minutes — a 5-minute event would
 *  otherwise render a few unclickable pixels tall. The event's real end is untouched. */
export const MIN_EVENT_MIN = 20;
/** Minutes in a day. DST days are 23/25h; the grid treats every day as 24×60 for placement,
 *  which is off by an hour only in the single transition day a year — acceptable for layout. */
export const DAY_MINUTES = 24 * 60;

const clamp = (n: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, n));

/** Local midnight of `d`. */
export const startOfLocalDay = (d: Date): Date =>
  new Date(d.getFullYear(), d.getMonth(), d.getDate());

/** Whether two Dates fall on the same local calendar day. */
export const isSameLocalDay = (a: Date, b: Date): boolean =>
  a.getFullYear() === b.getFullYear() &&
  a.getMonth() === b.getMonth() &&
  a.getDate() === b.getDate();

/** Minutes since local midnight, `0…1440`. */
export const minutesOfDay = (d: Date): number => d.getHours() * 60 + d.getMinutes();

/**
 * The `[startMin, endMin)` a timed event occupies **within one day column**, clamped to the
 * day and floored to {@link MIN_EVENT_MIN} of drawn height. An event that starts before this
 * day (a spill-over from a multi-day timed event) starts at 0; one that ends after it ends at
 * {@link DAY_MINUTES}. `day` is the column's date; the event is assumed bucketed onto it.
 */
export function eventDayBounds(
  ev: Pick<CalendarEvent, "start" | "end">,
  day: Date,
): { startMin: number; endMin: number } {
  const startMin = isSameLocalDay(ev.start, day) ? clamp(minutesOfDay(ev.start), 0, DAY_MINUTES) : 0;
  const rawEnd = isSameLocalDay(ev.end, day) ? clamp(minutesOfDay(ev.end), 0, DAY_MINUTES) : DAY_MINUTES;
  // A zero/negative or sub-floor span still draws MIN_EVENT_MIN tall (but never past the day).
  const endMin = clamp(Math.max(rawEnd, startMin + MIN_EVENT_MIN), 0, DAY_MINUTES);
  return { startMin, endMin };
}

/* ── overlap lane packing ──────────────────────────────────────────────────── */

export interface LaneInput {
  id: string;
  startMin: number;
  endMin: number;
}

export interface LaneBox extends LaneInput {
  /** 0-based column this event sits in within its overlap cluster. */
  lane: number;
  /** How many columns its cluster needs — every box in a cluster shares this, so widths align. */
  lanes: number;
}

/**
 * Assign each event a column so overlapping events sit side by side (the classic week-grid
 * layout). Events are grouped into **clusters** of transitively-overlapping intervals; within
 * a cluster each event greedily takes the first column whose previous occupant has already
 * ended (first-fit), and every event in the cluster is told the cluster's total column count so
 * the renderer can size each to `1 / lanes` of the day width. Input order is irrelevant — it
 * sorts by start, then end.
 */
export function layoutDayColumns(items: LaneInput[]): LaneBox[] {
  const sorted = [...items].sort((a, b) => a.startMin - b.startMin || a.endMin - b.endMin);
  const out: LaneBox[] = [];
  let cluster: LaneBox[] = [];
  let clusterEnd = -Infinity; // latest end among the current cluster's events

  const flush = () => {
    const laneEnds: number[] = []; // laneEnds[i] = end of the last event placed in column i
    for (const box of cluster) {
      let lane = laneEnds.findIndex((end) => end <= box.startMin);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(box.endMin);
      } else {
        laneEnds[lane] = box.endMin;
      }
      box.lane = lane;
    }
    for (const box of cluster) box.lanes = laneEnds.length;
    out.push(...cluster);
    cluster = [];
    clusterEnd = -Infinity;
  };

  for (const item of sorted) {
    // A gap (this event starts at/after everything so far ends) closes the cluster.
    if (cluster.length && item.startMin >= clusterEnd) flush();
    cluster.push({ ...item, lane: 0, lanes: 1 });
    clusterEnd = Math.max(clusterEnd, item.endMin);
  }
  flush();
  return out;
}

/* ── drag geometry ─────────────────────────────────────────────────────────── */

/** Snap a raw minute delta to the nearest {@link SNAP_MIN} step. */
export const snapMinutes = (rawMin: number, snap: number = SNAP_MIN): number =>
  Math.round(rawMin / snap) * snap;

/** Convert a vertical pixel delta into a snapped minute delta at the grid's density. */
export const pxToSnappedMinutes = (deltaPx: number, hourHeight: number = HOUR_HEIGHT): number =>
  snapMinutes((deltaPx / hourHeight) * 60);

/**
 * Rebuild a Date `deltaDays` days and `deltaMinutes` minutes from `base`, in **wall-clock**
 * terms — reconstructed from local calendar components, not millisecond arithmetic, so a
 * whole-day move keeps the same clock time across a DST boundary and an over-midnight minute
 * delta rolls the date correctly. Seconds/millis are dropped (the grid works in minutes).
 */
export function shiftWallClock(base: Date, deltaMinutes: number, deltaDays: number): Date {
  const total = minutesOfDay(base) + deltaMinutes;
  const dayRoll = Math.floor(total / DAY_MINUTES);
  const minOfDay = ((total % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES;
  return new Date(
    base.getFullYear(),
    base.getMonth(),
    base.getDate() + deltaDays + dayRoll,
    Math.floor(minOfDay / 60),
    minOfDay % 60,
    0,
    0,
  );
}

export type DragMode = "move" | "resize-end";

/**
 * The new `{start, end}` for a drag. **move** shifts both endpoints by the delta, preserving
 * the exact original duration; **resize-end** moves only the end, floored to
 * {@link MIN_EVENT_MIN} after the start so an event can't be dragged to zero/negative length.
 * `deltaDays` is only consulted for a move (kept 0 by the current vertical-only wiring, but
 * carried here so cross-day drag is a wiring change, not a math change).
 */
export function applyDrag(
  origStart: Date,
  origEnd: Date,
  mode: DragMode,
  deltaMinutes: number,
  deltaDays: number = 0,
): { start: Date; end: Date } {
  if (mode === "resize-end") {
    const shifted = shiftWallClock(origEnd, deltaMinutes, 0);
    const floor = new Date(origStart.getTime() + MIN_EVENT_MIN * 60_000);
    return { start: origStart, end: shifted.getTime() <= floor.getTime() ? floor : shifted };
  }
  const start = shiftWallClock(origStart, deltaMinutes, deltaDays);
  const end = new Date(start.getTime() + (origEnd.getTime() - origStart.getTime()));
  return { start, end };
}

/* ── persistence seam ──────────────────────────────────────────────────────── */

/**
 * The event's own action that can retime it — the "Edit" form action whose fields include both
 * `start` and `end` (a form with no explicit `fields` edits everything, so it qualifies too).
 * Returns `undefined` for a read-only event (no editable calendar action), which the grid reads
 * as "not draggable". Module-agnostic: it matches the shape of the action, never a tool name.
 */
export function findMoveAction(ev: Pick<CalendarEvent, "actions">): BoardAction | undefined {
  return (
    ev.actions.find(
      (a) => a.form && a.fields && a.fields.includes("start") && a.fields.includes("end"),
    ) ?? ev.actions.find((a) => a.form && !a.fields)
  );
}

/**
 * Serialize a dragged timed endpoint for `calendar_update_event`. An **offset-carrying** ISO
 * instant (`toISOString()`), matching what the web Edit form submits — so the event lands at
 * exactly the instant it was dragged to in the viewer's zone, with no naive-timezone ambiguity
 * (a bare local string would be re-read in the operator's *configured* zone, which need not be
 * the viewer's).
 */
export const serializeTimed = (d: Date): string => d.toISOString();

/** Format an hour-of-day (0–23) for the time gutter, in the viewer's locale (e.g. `9 AM`). */
export function formatHour(hour: number): string {
  const d = new Date(2000, 0, 1, hour, 0, 0);
  return d.toLocaleTimeString(undefined, { hour: "numeric" });
}
