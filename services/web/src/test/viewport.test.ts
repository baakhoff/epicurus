import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useViewportMirror } from "@/lib/viewport";

/** A minimal `VisualViewport` stand-in — jsdom doesn't implement the real one. */
class FakeVisualViewport extends EventTarget {
  height = 800;
  offsetTop = 0;
}

describe("useViewportMirror (#429, #476)", () => {
  let vv: FakeVisualViewport;

  beforeEach(() => {
    vv = new FakeVisualViewport();
    Object.defineProperty(window, "visualViewport", { value: vv, configurable: true });
  });

  afterEach(() => {
    document.documentElement.style.removeProperty("--app-height");
    document.documentElement.style.removeProperty("--app-offset-top");
  });

  it("mirrors the visual viewport's height and offset on mount", () => {
    vv.height = 700;
    renderHook(() => useViewportMirror());
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("700px");
    expect(document.documentElement.style.getPropertyValue("--app-offset-top")).toBe("0px");
  });

  it("mirrors a keyboard pan into --app-offset-top on a `scroll` event (#476 fallback)", () => {
    renderHook(() => useViewportMirror());
    // The keyboard opens on an engine that ignores interactive-widget=resizes-content: it
    // pans the visual viewport within an unchanged layout viewport instead of resizing it.
    // The visual viewport reports that pan via a `scroll` event, not `resize`.
    vv.height = 400;
    vv.offsetTop = 320;
    vv.dispatchEvent(new Event("scroll"));
    expect(document.documentElement.style.getPropertyValue("--app-offset-top")).toBe("320px");
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("400px");
  });

  it("stays at zero offset when the layout viewport resizes instead of panning", () => {
    renderHook(() => useViewportMirror());
    // The primary fix path: an engine that honors interactive-widget=resizes-content
    // resizes the layout viewport for the keyboard, so there is no pan to compensate for.
    vv.height = 400;
    vv.dispatchEvent(new Event("resize"));
    expect(document.documentElement.style.getPropertyValue("--app-offset-top")).toBe("0px");
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("400px");
  });

  it("falls back to window.innerHeight and a zero offset when there is no visualViewport", () => {
    Object.defineProperty(window, "visualViewport", { value: undefined, configurable: true });
    renderHook(() => useViewportMirror());
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe(
      `${window.innerHeight}px`,
    );
    expect(document.documentElement.style.getPropertyValue("--app-offset-top")).toBe("0px");
  });

  it("cleans up its visualViewport listeners on unmount", () => {
    const removeSpy = vi.spyOn(vv, "removeEventListener");
    const { unmount } = renderHook(() => useViewportMirror());
    unmount();
    expect(removeSpy).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(removeSpy).toHaveBeenCalledWith("scroll", expect.any(Function));
  });
});
