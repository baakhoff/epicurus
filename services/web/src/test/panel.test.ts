import { beforeEach, describe, expect, it } from "vitest";

import { usePanel } from "@/stores/panel";

beforeEach(() => usePanel.getState().close());

describe("panel store", () => {
  it("opens a view and exposes it as the current entry", () => {
    usePanel.getState().open("entity-detail", { title: "X" }, "Event");
    const { stack } = usePanel.getState();
    expect(stack).toHaveLength(1);
    expect(stack.at(-1)).toMatchObject({ view: "entity-detail", title: "Event" });
  });

  it("keeps a back-stack and pops with back()", () => {
    const { open, back } = usePanel.getState();
    open("entity-detail", {}, "A");
    open("email-reader", {}, "B");
    expect(usePanel.getState().stack).toHaveLength(2);
    back();
    expect(usePanel.getState().stack).toHaveLength(1);
    expect(usePanel.getState().stack.at(-1)?.title).toBe("A");
  });

  it("close() clears the whole stack", () => {
    const { open, close } = usePanel.getState();
    open("entity-detail", {}, "A");
    open("entity-detail", {}, "B");
    close();
    expect(usePanel.getState().stack).toEqual([]);
  });

  it("back() on an empty stack is a no-op", () => {
    usePanel.getState().back();
    expect(usePanel.getState().stack).toEqual([]);
  });
});
