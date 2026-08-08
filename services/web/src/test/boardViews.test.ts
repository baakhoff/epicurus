/**
 * Unit tests for the board representations' pure math (#767) — flattening/dedupe, List
 * sorting, and the Calendar month geometry — deterministic, no DOM (the rendering side
 * is covered in BoardView.test.tsx).
 */
import { describe, expect, it } from "vitest";

import {
  cardsByDate,
  dueTone,
  flattenCards,
  monthCells,
  monthOf,
  sortCards,
  stepMonth,
  ymd,
} from "@/components/archetypes/boardViews";
import { BoardCard, BoardColumn } from "@/lib/contracts";

/** A minimal parsed card — through the zod schema so defaults match production. */
function card(
  id: string,
  fields: Partial<{
    title: string;
    due: string | null;
    priority: string | null;
    tags: string[];
    list_title: string | null;
    done: boolean;
  }> = {},
): BoardCard {
  return BoardCard.parse({ id, title: fields.title ?? id, ...fields });
}

const column = (id: string, cards: BoardCard[]): BoardColumn =>
  BoardColumn.parse({ id, title: id, cards });

describe("flattenCards", () => {
  it("flattens columns in order and dedupes by id, first appearance winning", () => {
    const a = card("a");
    const b = card("b");
    const aAgain = card("a", { title: "a-duplicate" });
    const flat = flattenCards([column("one", [a, b]), column("two", [aAgain])]);
    expect(flat.map((c) => c.id)).toEqual(["a", "b"]);
    expect(flat[0].title).toBe("a"); // the first appearance, not the duplicate
  });
});

describe("sortCards", () => {
  const dated = card("dated", { due: "2026-06-01" });
  const later = card("later", { due: "2026-07-01" });
  const undated = card("undated", { due: null });

  it("sorts by due ascending and descending, missing always last", () => {
    const asc = sortCards([undated, later, dated], { key: "due", dir: 1 });
    expect(asc.map((c) => c.id)).toEqual(["dated", "later", "undated"]);
    const desc = sortCards([undated, later, dated], { key: "due", dir: -1 });
    expect(desc.map((c) => c.id)).toEqual(["later", "dated", "undated"]);
  });

  it("ranks priorities high → medium → low, none last", () => {
    const cards = [
      card("none", { priority: null }),
      card("low", { priority: "low" }),
      card("high", { priority: "high" }),
      card("medium", { priority: "medium" }),
    ];
    const asc = sortCards(cards, { key: "priority", dir: 1 });
    expect(asc.map((c) => c.id)).toEqual(["high", "medium", "low", "none"]);
  });

  it("is stable: equal keys keep the fetched order", () => {
    const first = card("first", { due: "2026-06-01" });
    const second = card("second", { due: "2026-06-01" });
    const third = card("third", { due: "2026-06-01" });
    const sorted = sortCards([first, second, third], { key: "due", dir: 1 });
    expect(sorted.map((c) => c.id)).toEqual(["first", "second", "third"]);
  });

  it("sorts titles case-insensitively and lists by their label", () => {
    const byTitle = sortCards(
      [card("b", { title: "banana" }), card("a", { title: "Apple" })],
      { key: "title", dir: 1 },
    );
    expect(byTitle.map((c) => c.id)).toEqual(["a", "b"]);

    const byList = sortCards(
      [card("w", { list_title: "Work" }), card("p", { list_title: "personal" }), card("n")],
      { key: "list", dir: 1 },
    );
    expect(byList.map((c) => c.id)).toEqual(["p", "w", "n"]);
  });

  it("slices a datetime due to its date for comparison", () => {
    const stamp = card("stamp", { due: "2026-06-01T09:00:00.000Z" });
    const plain = card("plain", { due: "2026-06-02" });
    const sorted = sortCards([plain, stamp], { key: "due", dir: 1 });
    expect(sorted.map((c) => c.id)).toEqual(["stamp", "plain"]);
  });
});

describe("month geometry", () => {
  it("builds a fixed 42-cell Monday-start grid", () => {
    // June 2026 starts on a Monday — no leading pad; the grid runs 1 Jun … 12 Jul.
    const cells = monthCells({ year: 2026, month: 6 });
    expect(cells).toHaveLength(42);
    expect(cells[0]).toEqual({ date: "2026-06-01", day: 1, inMonth: true });
    expect(cells[29]).toEqual({ date: "2026-06-30", day: 30, inMonth: true });
    expect(cells[30]).toEqual({ date: "2026-07-01", day: 1, inMonth: false });
    expect(cells[41].date).toBe("2026-07-12");
  });

  it("pads the leading days from the previous month", () => {
    // July 2026 starts on a Wednesday → two leading June cells (Mon 29, Tue 30).
    const cells = monthCells({ year: 2026, month: 7 });
    expect(cells[0]).toEqual({ date: "2026-06-29", day: 29, inMonth: false });
    expect(cells[2]).toEqual({ date: "2026-07-01", day: 1, inMonth: true });
  });

  it("steps months across year boundaries", () => {
    expect(stepMonth({ year: 2026, month: 12 }, 1)).toEqual({ year: 2027, month: 1 });
    expect(stepMonth({ year: 2026, month: 1 }, -1)).toEqual({ year: 2025, month: 12 });
    expect(stepMonth({ year: 2026, month: 6 }, 1)).toEqual({ year: 2026, month: 7 });
  });

  it("monthOf/ymd read local dates", () => {
    const d = new Date(2026, 5, 14); // 14 Jun 2026 local
    expect(monthOf(d)).toEqual({ year: 2026, month: 6 });
    expect(ymd(d)).toBe("2026-06-14");
  });
});

describe("cardsByDate", () => {
  it("keys cards by their due date, slicing datetimes, dropping undated", () => {
    const map = cardsByDate([
      card("a", { due: "2026-06-14" }),
      card("b", { due: "2026-06-14T22:00:00.000Z" }),
      card("c", { due: null }),
    ]);
    expect([...map.keys()]).toEqual(["2026-06-14"]);
    expect(map.get("2026-06-14")!.map((c) => c.id)).toEqual(["a", "b"]);
  });
});

describe("dueTone", () => {
  const TODAY = "2026-06-14";

  it("matches the board's due-badge tone language", () => {
    expect(dueTone(card("late", { due: "2026-06-01" }), TODAY)).toBe("overdue");
    expect(dueTone(card("now", { due: TODAY }), TODAY)).toBe("today");
    expect(dueTone(card("soon", { due: "2026-07-01" }), TODAY)).toBe("upcoming");
    expect(dueTone(card("finished", { due: "2026-06-01", done: true }), TODAY)).toBe("done");
  });
});
