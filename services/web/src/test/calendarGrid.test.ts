import { describe, expect, it } from "vitest";

import {
  applyDrag,
  eventDayBounds,
  findMoveAction,
  formatHour,
  layoutDayColumns,
  minutesOfDay,
  MIN_EVENT_MIN,
  pxToSnappedMinutes,
  serializeTimed,
  shiftWallClock,
  snapMinutes,
  HOUR_HEIGHT,
} from "@/components/archetypes/calendarGrid";
import type { BoardAction } from "@/lib/contracts";

const at = (h: number, m = 0) => new Date(2026, 5, 15, h, m, 0);

describe("minutesOfDay / eventDayBounds", () => {
  it("reads minutes since local midnight", () => {
    expect(minutesOfDay(at(9, 30))).toBe(570);
    expect(minutesOfDay(at(0, 0))).toBe(0);
  });

  it("bounds a same-day timed event to its minutes", () => {
    expect(eventDayBounds({ start: at(9), end: at(10, 30) }, at(0))).toEqual({
      startMin: 540,
      endMin: 630,
    });
  });

  it("floors a very short event to a grab-able height without moving its start", () => {
    const { startMin, endMin } = eventDayBounds({ start: at(9), end: at(9, 5) }, at(0));
    expect(startMin).toBe(540);
    expect(endMin).toBe(540 + MIN_EVENT_MIN);
  });

  it("clamps an event that spills past midnight to the end of the day column", () => {
    // start today 23:00, ends tomorrow 01:00 → shows 23:00→24:00 in today's column.
    const end = new Date(2026, 5, 16, 1, 0, 0);
    expect(eventDayBounds({ start: at(23), end }, at(0))).toEqual({ startMin: 1380, endMin: 1440 });
  });

  it("starts a spilled-in event at the top of the day", () => {
    // started yesterday, ends today 02:00 → shows 00:00→02:00 in today's column.
    const start = new Date(2026, 5, 14, 22, 0, 0);
    expect(eventDayBounds({ start, end: at(2) }, at(0))).toEqual({ startMin: 0, endMin: 120 });
  });
});

describe("layoutDayColumns", () => {
  it("gives a lone event the full width", () => {
    expect(layoutDayColumns([{ id: "a", startMin: 540, endMin: 600 }])).toEqual([
      { id: "a", startMin: 540, endMin: 600, lane: 0, lanes: 1 },
    ]);
  });

  it("keeps sequential, non-overlapping events in one lane each in separate clusters", () => {
    const out = layoutDayColumns([
      { id: "a", startMin: 540, endMin: 600 },
      { id: "b", startMin: 600, endMin: 660 }, // starts exactly when a ends — no overlap
    ]);
    expect(out.every((b) => b.lane === 0 && b.lanes === 1)).toBe(true);
  });

  it("splits two overlapping events into two lanes", () => {
    const out = layoutDayColumns([
      { id: "a", startMin: 540, endMin: 660 },
      { id: "b", startMin: 600, endMin: 720 },
    ]);
    const a = out.find((x) => x.id === "a")!;
    const b = out.find((x) => x.id === "b")!;
    expect(a.lanes).toBe(2);
    expect(b.lanes).toBe(2);
    expect(new Set([a.lane, b.lane])).toEqual(new Set([0, 1]));
  });

  it("reuses a freed lane (first-fit) inside one cluster", () => {
    // a 9–10, b 9–11 (overlaps a), c 10–11 (overlaps b, not a) → c reuses a's lane 0.
    const out = layoutDayColumns([
      { id: "a", startMin: 540, endMin: 600 },
      { id: "b", startMin: 540, endMin: 660 },
      { id: "c", startMin: 600, endMin: 660 },
    ]);
    const by = Object.fromEntries(out.map((b) => [b.id, b]));
    expect(by.a.lanes).toBe(2); // the whole cluster needs 2 columns
    expect(by.c.lane).toBe(by.a.lane); // c slots back into the column a vacated
    expect(by.b.lane).not.toBe(by.a.lane);
  });

  it("is independent of input order", () => {
    const forward = layoutDayColumns([
      { id: "a", startMin: 540, endMin: 660 },
      { id: "b", startMin: 600, endMin: 720 },
    ]);
    const reverse = layoutDayColumns([
      { id: "b", startMin: 600, endMin: 720 },
      { id: "a", startMin: 540, endMin: 660 },
    ]);
    expect(new Map(forward.map((b) => [b.id, b.lane]))).toEqual(
      new Map(reverse.map((b) => [b.id, b.lane])),
    );
  });
});

describe("snapping + wall-clock shift", () => {
  it("snaps to the quarter hour", () => {
    expect(snapMinutes(7)).toBe(0);
    expect(snapMinutes(8)).toBe(15);
    expect(snapMinutes(52)).toBe(45);
  });

  it("converts pixels to snapped minutes at the grid density", () => {
    expect(pxToSnappedMinutes(HOUR_HEIGHT)).toBe(60); // one hour row = 60 min
    expect(pxToSnappedMinutes(HOUR_HEIGHT / 4)).toBe(15); // a quarter row snaps to 15
  });

  it("shifts wall-clock time, preserving the hour across a whole-day move", () => {
    const shifted = shiftWallClock(at(10, 0), 0, 1);
    expect(shifted.getDate()).toBe(16);
    expect(shifted.getHours()).toBe(10); // same clock time, next day
  });

  it("rolls the date when a minute delta crosses midnight", () => {
    const shifted = shiftWallClock(at(23, 30), 60, 0); // +1h from 23:30 → 00:30 next day
    expect(shifted.getDate()).toBe(16);
    expect(shifted.getHours()).toBe(0);
    expect(shifted.getMinutes()).toBe(30);
  });
});

describe("applyDrag", () => {
  it("moves both endpoints, preserving duration", () => {
    const { start, end } = applyDrag(at(9), at(10), "move", 90);
    expect(minutesOfDay(start)).toBe(630); // 10:30
    expect(minutesOfDay(end)).toBe(690); // 11:30 — duration (60m) preserved
  });

  it("resizes only the end", () => {
    const { start, end } = applyDrag(at(9), at(10), "resize-end", 30);
    expect(start).toEqual(at(9));
    expect(minutesOfDay(end)).toBe(630); // 10:30
  });

  it("floors a resize so an event can't collapse to zero length", () => {
    const { start, end } = applyDrag(at(9), at(10), "resize-end", -600); // yank the end way up
    expect(start).toEqual(at(9));
    expect(end.getTime() - start.getTime()).toBe(MIN_EVENT_MIN * 60_000);
  });
});

describe("findMoveAction / serializeTimed / formatHour", () => {
  const edit: BoardAction = {
    tool: "calendar_update_event",
    label: "Edit",
    intent: "default",
    args: { event_id: "e1" },
    form: true,
    fields: ["title", "start", "end", "location"],
    form_values: {},
    icon_only: false,
  };
  const del: BoardAction = {
    tool: "calendar_delete_event",
    label: "Delete",
    intent: "danger",
    args: { event_id: "e1" },
    form: false,
    form_values: {},
    confirm: "Delete?",
    icon_only: false,
  };

  it("finds the edit action that can set start and end", () => {
    expect(findMoveAction({ actions: [del, edit] })).toBe(edit);
  });

  it("returns undefined for a read-only event", () => {
    expect(findMoveAction({ actions: [del] })).toBeUndefined();
    expect(findMoveAction({ actions: [] })).toBeUndefined();
  });

  it("serializes a timed endpoint as an offset-carrying instant", () => {
    expect(serializeTimed(new Date(Date.UTC(2026, 5, 15, 9, 0, 0)))).toBe("2026-06-15T09:00:00.000Z");
  });

  it("formats an hour label as a non-empty string", () => {
    expect(formatHour(9)).toMatch(/\d/);
  });
});
