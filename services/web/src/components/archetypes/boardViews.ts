/**
 * Pure helpers for the `board` archetype's alternate representations (#767) — the List
 * and Calendar views over the *same* board payload. Framework-free so flattening,
 * sorting, and month-grid placement get fast, deterministic unit coverage without a DOM;
 * {@link BoardView} owns the rendering and only calls in here.
 *
 * The module supplies data and the shell renders (ADR-0018/0049) — none of this touches
 * the module contract. The views read the cards' structured fields (`due` / `priority` /
 * `tags` / `list_title`, #767), never the rendered badge strings.
 */
import type { BoardCard, BoardColumn } from "@/lib/contracts";

/** The reserved control id that switches the board's client-side representation (#767). */
export const VIEW_CONTROL_ID = "view";
export const BOARD_VIEW = "board";
export const LIST_VIEW = "list";
export const CALENDAR_VIEW = "calendar";

/**
 * Flatten the module's grouped columns into one card list, **deduped by id** with first
 * appearance winning (preserving the module's order). A grouping may place one card
 * under several columns (multi-membership — e.g. a task under each of its tags), and the
 * flat representations must show each card exactly once.
 */
export function flattenCards(columns: BoardColumn[]): BoardCard[] {
  const seen = new Set<string>();
  const out: BoardCard[] = [];
  for (const column of columns) {
    for (const card of column.cards) {
      if (seen.has(card.id)) continue;
      seen.add(card.id);
      out.push(card);
    }
  }
  return out;
}

/* ── List view: column sorting ─────────────────────────────────────────────── */

export type ListSortKey = "title" | "due" | "priority" | "list";
export interface ListSort {
  key: ListSortKey;
  dir: 1 | -1;
}

/** Priority ranks ascend from most urgent, so an ascending sort leads with "high". An
 *  unknown value ranks after the known ones but before "none" (still *a* priority). */
const PRIORITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

/**
 * Sort cards for the List view. **Stable**: equal keys keep the fetched order
 * (`Array.prototype.sort` is spec-stable), so re-sorting never shuffles ties. A missing
 * value (no due date, no priority, no list) always sorts **last**, in either direction —
 * "show me by due date" should lead with the dated rows, not strand them behind blanks.
 */
export function sortCards(cards: BoardCard[], sort: ListSort): BoardCard[] {
  const value = (card: BoardCard): string | number | null => {
    if (sort.key === "title") return card.title.toLocaleLowerCase();
    if (sort.key === "due") return card.due ? card.due.slice(0, 10) : null;
    if (sort.key === "list") return card.list_title?.toLocaleLowerCase() ?? null;
    return card.priority == null ? null : (PRIORITY_RANK[card.priority] ?? 3);
  };
  return [...cards].sort((a, b) => {
    const av = value(a);
    const bv = value(b);
    if (av === bv) return 0;
    if (av === null) return 1; // missing always last, independent of direction
    if (bv === null) return -1;
    return (av < bv ? -1 : 1) * sort.dir;
  });
}

/* ── Calendar view: month geometry (local dates, Monday-start like the calendar
 *    archetype) ─────────────────────────────────────────────────────────────── */

/** A month identified by plain numbers (month is 1–12) — no Date arithmetic pitfalls. */
export interface MonthCursor {
  year: number;
  month: number;
}

export interface MonthCell {
  /** The cell's floating date, `YYYY-MM-DD` — matches the cards' `due` shape. */
  date: string;
  /** Day-of-month number the cell displays. */
  day: number;
  /** False for the leading/trailing days padding the 6-week grid. */
  inMonth: boolean;
}

const pad = (n: number): string => String(n).padStart(2, "0");

/** Local floating `YYYY-MM-DD` — never `toISOString()`, which would UTC-shift a date
 *  near local midnight (the same rule CalendarView's `ymd` follows). */
export const ymd = (d: Date): string =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

export const monthOf = (d: Date): MonthCursor => ({ year: d.getFullYear(), month: d.getMonth() + 1 });

export function stepMonth(cursor: MonthCursor, dir: 1 | -1): MonthCursor {
  const month = cursor.month + dir;
  if (month < 1) return { year: cursor.year - 1, month: 12 };
  if (month > 12) return { year: cursor.year + 1, month: 1 };
  return { year: cursor.year, month };
}

export function monthLabel(cursor: MonthCursor): string {
  return new Date(cursor.year, cursor.month - 1, 1).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
}

/** The fixed 6-week (42-cell) month grid, Monday-start — same geometry as the calendar
 *  archetype's month view, so the two pages read identically. */
export function monthCells(cursor: MonthCursor): MonthCell[] {
  const first = new Date(cursor.year, cursor.month - 1, 1);
  const lead = (first.getDay() + 6) % 7; // cells before the 1st (Monday = 0)
  return Array.from({ length: 42 }, (_, i) => {
    const d = new Date(cursor.year, cursor.month - 1, 1 - lead + i);
    return { date: ymd(d), day: d.getDate(), inMonth: d.getMonth() === cursor.month - 1 };
  });
}

/** Cards keyed by their due date (`YYYY-MM-DD`). Undated cards are dropped — they never
 *  appear on a calendar (the tasks module keeps them off this page entirely, #766). */
export function cardsByDate(cards: BoardCard[]): Map<string, BoardCard[]> {
  const map = new Map<string, BoardCard[]>();
  for (const card of cards) {
    if (!card.due) continue;
    const key = card.due.slice(0, 10);
    const bucket = map.get(key);
    if (bucket) bucket.push(card);
    else map.set(key, [card]);
  }
  return map;
}

/** The tone class a card's chip/due-cell carries — the board's due-badge tone language
 *  (overdue → danger, today → accent), date-generic so no module semantics leak in. */
export type DueTone = "done" | "overdue" | "today" | "upcoming";

export function dueTone(card: Pick<BoardCard, "due" | "done">, today: string): DueTone {
  if (card.done) return "done";
  if (!card.due) return "upcoming";
  const due = card.due.slice(0, 10);
  if (due < today) return "overdue";
  if (due === today) return "today";
  return "upcoming";
}
