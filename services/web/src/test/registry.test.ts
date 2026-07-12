import { describe, expect, it } from "vitest";

import { modulePageNavs, modulePagePath, reviewPageNavs, sortByPageOrder } from "@/app/registry";
import { ModuleSnapshot } from "@/lib/contracts";

function snapshot(
  name: string,
  healthy: boolean,
  pages: Array<{ id: string; title: string; nav_order?: number; archetype?: string }>,
  enabled = true,
) {
  return ModuleSnapshot.parse({
    manifest: {
      name,
      version: "1.0.0",
      pages: pages.map((p) => ({ archetype: "browser", ...p })),
    },
    status: { healthy },
    enabled,
  });
}

describe("modulePageNavs", () => {
  it("derives nav entries from reachable modules' pages, sorted by nav_order then label", () => {
    const navs = modulePageNavs([
      snapshot("b", true, [{ id: "p", title: "Beta", nav_order: 20 }]),
      snapshot("a", true, [{ id: "p", title: "Alpha", nav_order: 10 }]),
      snapshot("c", true, [{ id: "p", title: "Gamma", nav_order: 10 }]),
    ]);
    expect(navs.map((n) => n.label)).toEqual(["Alpha", "Gamma", "Beta"]);
    expect(navs[0].path).toBe(modulePagePath("a", "p"));
  });

  it("omits pages from unreachable modules (they cannot serve data)", () => {
    const navs = modulePageNavs([snapshot("down", false, [{ id: "p", title: "Hidden" }])]);
    expect(navs).toEqual([]);
  });

  it("omits pages from a disabled module — hidden from the nav (#126)", () => {
    const navs = modulePageNavs([snapshot("off", true, [{ id: "p", title: "Hidden" }], false)]);
    expect(navs).toEqual([]);
  });

  it("returns nothing for a module with no pages", () => {
    expect(modulePageNavs([snapshot("plain", true, [])])).toEqual([]);
  });
});

describe("reviewPageNavs", () => {
  it("returns only the review-archetype pages (what the Suggestions inbox aggregates)", () => {
    const navs = reviewPageNavs([
      snapshot("knowledge", true, [
        { id: "vault", title: "Knowledge", archetype: "editor", nav_order: 30 },
        { id: "review", title: "Suggestions", archetype: "review", nav_order: 31 },
      ]),
      snapshot("notes", true, [{ id: "notes", title: "Notes", archetype: "editor" }]),
    ]);
    expect(navs.map((n) => n.module)).toEqual(["knowledge"]);
    expect(navs[0].pageId).toBe("review");
    expect(navs[0].archetype).toBe("review");
  });

  it("omits review pages from unreachable or disabled modules", () => {
    const down = reviewPageNavs([
      snapshot("k", false, [{ id: "review", title: "Suggestions", archetype: "review" }]),
    ]);
    const off = reviewPageNavs([
      snapshot("k", true, [{ id: "review", title: "Suggestions", archetype: "review" }], false),
    ]);
    expect(down).toEqual([]);
    expect(off).toEqual([]);
  });
});

describe("sortByPageOrder (#543)", () => {
  const modules = [
    snapshot("a", true, [{ id: "p", title: "Alpha", nav_order: 10 }]),
    snapshot("b", true, [{ id: "p", title: "Beta", nav_order: 20 }]),
    snapshot("c", true, [{ id: "p", title: "Gamma", nav_order: 30 }]),
  ];
  const [a, b, c] = modulePageNavs(modules);

  it("returns the default order unchanged when no preference is set", () => {
    expect(sortByPageOrder(modulePageNavs(modules), [])).toEqual([a, b, c]);
  });

  it("sorts ordered pages by their position in the preference (ordered ∪ ...)", () => {
    const result = sortByPageOrder(modulePageNavs(modules), [c.path, a.path, b.path]);
    expect(result.map((p) => p.label)).toEqual(["Gamma", "Alpha", "Beta"]);
  });

  it("appends a page missing from the preference after every ordered page, never hiding it (... ∪ unknown ∪ ...)", () => {
    // Only "b" has an explicit preference; "a" and "c" are unknown to it and keep their
    // modulePageNavs-relative order (nav_order 10 < 30), appended after "b".
    const result = sortByPageOrder(modulePageNavs(modules), [b.path]);
    expect(result.map((p) => p.label)).toEqual(["Beta", "Alpha", "Gamma"]);
  });

  it("ignores a stale id with no matching live page, as inert rather than fatal (... ∪ stale)", () => {
    const result = sortByPageOrder(modulePageNavs(modules), [
      "/m/ghost/gone",
      c.path,
      a.path,
      b.path,
    ]);
    expect(result.map((p) => p.label)).toEqual(["Gamma", "Alpha", "Beta"]);
  });

  it("restores a page's remembered position once its module is re-enabled", () => {
    const order = [c.path, a.path, b.path];
    // "b"'s module goes down: modulePageNavs no longer yields it, but its id stays in `order`
    // untouched (the store is never pruned) — sortByPageOrder simply has nothing to place it at.
    const disabled = [modules[0], snapshot("b", false, [{ id: "p", title: "Beta" }]), modules[2]];
    const whileDown = sortByPageOrder(modulePageNavs(disabled), order);
    expect(whileDown.map((p) => p.label)).toEqual(["Gamma", "Alpha"]);
    // Back up: "b" reappears at exactly its old position, third in `order`, with no special
    // re-enable handling required.
    const backUp = sortByPageOrder(modulePageNavs(modules), order);
    expect(backUp.map((p) => p.label)).toEqual(["Gamma", "Alpha", "Beta"]);
  });
});
