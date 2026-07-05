import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { epFetch } from "@/lib/http";
import { useConnection, useConnectionWatch } from "@/stores/connection";

// The shell's one connection signal (#494): epFetch turns every /platform request the
// app already makes into reachability evidence — no dedicated probe endpoint — and
// useConnectionWatch shapes recovery around events, never a new poll.

const reset = () => useConnection.setState({ online: true, coreDown: false });

describe("epFetch connectivity evidence (#494)", () => {
  beforeEach(reset);
  afterEach(() => vi.unstubAllGlobals());

  it("marks the core unreachable on a network-level failure and rethrows", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(epFetch("/platform/v1/power")).rejects.toBeInstanceOf(TypeError);
    expect(useConnection.getState().coreDown).toBe(true);
  });

  it("treats an aborted request as no evidence either way", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("gone", "AbortError")));
    await expect(epFetch("/x")).rejects.toThrow("gone");
    expect(useConnection.getState().coreDown).toBe(false);
  });

  it("marks unreachable on a gateway 502/504 — nginx answered, the core did not", async () => {
    for (const status of [502, 504]) {
      reset();
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("bad gateway", { status })));
      await epFetch("/x");
      expect(useConnection.getState().coreDown, `status ${status}`).toBe(true);
    }
  });

  it("counts ANY other answer as reachable — errors and the paused 503 included", async () => {
    // A 404/500 proves epicurus answered; 503 is the *paused* state (PausedError), a
    // mood rather than an outage — it must never light the unreachable banner.
    for (const status of [200, 404, 500, 503]) {
      useConnection.setState({ online: true, coreDown: true });
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status })));
      await epFetch("/x");
      expect(useConnection.getState().coreDown, `status ${status}`).toBe(false);
    }
  });
});

describe("useConnectionWatch (#494)", () => {
  beforeEach(reset);

  it("mirrors the browser online/offline events and re-checks vitals on return", () => {
    const refetchVitals = vi.fn();
    renderHook(() => useConnectionWatch({ refetchVitals, onRecovered: vi.fn() }));

    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    expect(useConnection.getState().online).toBe(false);
    expect(refetchVitals).not.toHaveBeenCalled();

    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    expect(useConnection.getState().online).toBe(true);
    expect(refetchVitals).toHaveBeenCalledTimes(1); // the network may be back — check now
  });

  it("re-checks immediately when the tab becomes visible while unreachable", () => {
    const refetchVitals = vi.fn();
    renderHook(() => useConnectionWatch({ refetchVitals, onRecovered: vi.fn() }));
    act(() => useConnection.getState().reportUnreachable());

    // jsdom tabs are always "visible" — the event alone models returning to the tab.
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(refetchVitals).toHaveBeenCalledTimes(1);
  });

  it("spends nothing on visibility changes while everything is reachable", () => {
    const refetchVitals = vi.fn();
    renderHook(() => useConnectionWatch({ refetchVitals, onRecovered: vi.fn() }));
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(refetchVitals).not.toHaveBeenCalled();
  });

  it("fires onRecovered exactly once per outage, on the down→up transition", () => {
    const onRecovered = vi.fn();
    renderHook(() => useConnectionWatch({ refetchVitals: vi.fn(), onRecovered }));

    act(() => useConnection.getState().reportUnreachable());
    expect(onRecovered).not.toHaveBeenCalled();

    act(() => useConnection.getState().reportReachable());
    expect(onRecovered).toHaveBeenCalledTimes(1);

    // reportReachable fires on every healthy response — only the transition may count,
    // or recovery would invalidate the query cache in a storm.
    act(() => useConnection.getState().reportReachable());
    expect(onRecovered).toHaveBeenCalledTimes(1);
  });
});
