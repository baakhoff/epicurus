import { describe, expect, it } from "vitest";

import { diffLines, mergeHunks, toHunks } from "@/lib/linediff";

describe("linediff", () => {
  const before = "a\nb\nc\n";
  const after = "a\nB\nc\nd\n";

  it("accepting all hunks reproduces the proposal", () => {
    const diff = diffLines(before, after);
    const all = new Set(toHunks(diff).map((h) => h.id));
    expect(mergeHunks(diff, all)).toBe(after);
  });

  it("accepting no hunks reproduces the current document", () => {
    const diff = diffLines(before, after);
    expect(mergeHunks(diff, new Set())).toBe(before);
  });

  it("accepting only one hunk applies just that change", () => {
    // a/b/c → a/B/c/d is two hunks: the b→B substitution and the appended d.
    const diff = diffLines("a\nb\nc", "a\nB\nc\nd");
    const hunks = toHunks(diff);
    expect(hunks).toHaveLength(2);
    expect(mergeHunks(diff, new Set([hunks[0].id]))).toBe("a\nB\nc"); // keep b→B, drop +d
    expect(mergeHunks(diff, new Set([hunks[1].id]))).toBe("a\nb\nc\nd"); // keep +d, drop b→B
  });

  it("handles a create (empty current)", () => {
    const diff = diffLines("", "x\ny\n");
    expect(mergeHunks(diff, new Set([0]))).toBe("x\ny\n");
    expect(mergeHunks(diff, new Set())).toBe("");
  });

  it("handles a delete-to-empty", () => {
    const diff = diffLines("x\ny\n", "");
    expect(mergeHunks(diff, new Set([0]))).toBe("");
    expect(mergeHunks(diff, new Set())).toBe("x\ny\n");
  });

  it("an unchanged document produces no hunks", () => {
    const diff = diffLines("same\n", "same\n");
    expect(toHunks(diff)).toHaveLength(0);
    expect(mergeHunks(diff, new Set())).toBe("same\n");
  });
});
