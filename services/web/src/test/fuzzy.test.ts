import { describe, expect, it } from "vitest";

import { fuzzyScore, rankFiltered } from "@/lib/fuzzy";

describe("fuzzyScore (#491)", () => {
  it("matches subsequences case-insensitively and rejects everything else", () => {
    expect(fuzzyScore("cal", "Calendar")).not.toBeNull();
    expect(fuzzyScore("CAL", "calendar")).not.toBeNull();
    expect(fuzzyScore("mdl", "Models")).not.toBeNull(); // m·o·D·e·L·s
    expect(fuzzyScore("xyz", "Calendar")).toBeNull();
    expect(fuzzyScore("calz", "Calendar")).toBeNull(); // every needle char must land
  });

  it("treats the empty query as an indifferent match-all", () => {
    expect(fuzzyScore("", "anything")).toBe(0);
    expect(fuzzyScore("   ", "anything")).toBe(0);
  });

  it("skips spaces in the needle so multi-word fragments work", () => {
    expect(fuzzyScore("new ch", "New chat")).not.toBeNull();
    expect(fuzzyScore("ne ch", "New chat")).not.toBeNull();
  });

  it("ranks consecutive runs above scattered letters", () => {
    const consecutive = fuzzyScore("cal", "Calendar")!;
    const scattered = fuzzyScore("cal", "Cavalcade")!;
    expect(consecutive).toBeGreaterThan(scattered);
  });

  it("rewards word-boundary starts", () => {
    const boundary = fuzzyScore("se", "Sessions")!;
    const midWord = fuzzyScore("se", "Closet")!;
    expect(boundary).toBeGreaterThan(midWord);
  });

  it("prefers early matches over buried ones", () => {
    const early = fuzzyScore("chat", "Chat")!;
    const buried = fuzzyScore("chat", "Files chat log")!;
    expect(early).toBeGreaterThan(buried);
  });
});

describe("rankFiltered (#491)", () => {
  const items = [
    { label: "Chat" },
    { label: "Calendar" },
    { label: "Local mail" },
    { label: "Settings" },
  ];

  it("drops non-matches and orders by score", () => {
    const out = rankFiltered(items, "cal", (i) => i.label).map((i) => i.label);
    expect(out[0]).toBe("Calendar"); // word-start consecutive run wins
    expect(out).toContain("Local mail"); // l·o·C·A·L — a subsequence, ranked lower
    expect(out).not.toContain("Settings");
  });

  it("keeps the incoming order on ties (recency/nav order is the tiebreak)", () => {
    const dupes = [{ label: "untitled" }, { label: "untitled" }];
    const out = rankFiltered(dupes, "unt", (i) => i.label);
    expect(out[0]).toBe(dupes[0]);
    expect(out[1]).toBe(dupes[1]);
  });

  it("returns everything, untouched, for the empty query", () => {
    const out = rankFiltered(items, "", (i) => i.label).map((i) => i.label);
    expect(out).toEqual(["Chat", "Calendar", "Local mail", "Settings"]);
  });
});
