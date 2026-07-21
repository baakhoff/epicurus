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

  describe("replace()", () => {
    it("swaps the payload, keeping the current entry's title when none is given (#659)", () => {
      usePanel.getState().open("email-reader", { id: 1 }, "Original");
      usePanel.getState().replace({ id: 2 });
      const top = usePanel.getState().stack.at(-1);
      expect(top).toMatchObject({ payload: { id: 2 }, title: "Original" });
    });

    it("swaps the title too when one is given (#659) — was silently dropped before", () => {
      usePanel.getState().open("email-reader", { id: 1 }, "Original");
      usePanel.getState().replace({ id: 2 }, "Fresh title");
      const top = usePanel.getState().stack.at(-1);
      expect(top).toMatchObject({ payload: { id: 2 }, title: "Fresh title" });
    });

    it("is a no-op on an empty stack", () => {
      usePanel.getState().replace({ id: 1 }, "Ignored");
      expect(usePanel.getState().stack).toEqual([]);
    });
  });
});
