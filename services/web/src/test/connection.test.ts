import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { epFetch } from "@/lib/http";
import {
  UNREACHABLE_GRACE_MS,
  useConnection,
  useConnectionWatch,
  type UnreachableEvidence,
  type UnreachableKind,
} from "@/stores/connection";

// The shell's connection signal (#494, debounced #791): epFetch turns every /platform
// request the app already makes into reachability evidence — no dedicated probe endpoint
// — and useConnectionWatch shapes recovery (and the one confirming re-probe) around
// events, never a new poll. `coreDown` itself stays eager and single-strike, exactly as
// before — it gates send-adjacent actions that would rather refuse instantly than let one
// through a debounce window only to have it fail anyway. `pendingDown`/`confirmedDown`
// are the new pair the banner alone renders on (App.tsx's ConnectionBanner).

function evidence(kind: UnreachableKind, path = "/platform/v1/power"): UnreachableEvidence {
  return { method: "GET", path, kind };
}

const reset = () =>
  useConnection.setState({
    online: true,
    coreDown: false,
    pendingDown: false,
    confirmedDown: false,
    lastEvidence: null,
  });

// Fake timers everywhere in this file, not just the debounce block: reportUnreachable()
// always schedules a real grace-window setTimeout on a first failure, and several tests
// below (and in http.ts's own evidence tests) call it without ever advancing past the
// grace or recovering — with real timers that handle would still be live 5s later,
// leaking into whatever test runs next. clearAllTimers()+useRealTimers() in afterEach
// guarantees none of them ever actually fire.
beforeEach(() => {
  vi.useFakeTimers();
  reset();
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe("epFetch connectivity evidence (#494)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("marks the core unreachable on a network-level failure and rethrows", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(epFetch("/platform/v1/power")).rejects.toBeInstanceOf(TypeError);
    expect(useConnection.getState().coreDown).toBe(true);
    expect(useConnection.getState().pendingDown).toBe(true);
    expect(useConnection.getState().lastEvidence).toEqual({
      method: "GET",
      path: "/platform/v1/power",
      kind: "TypeError",
    });
  });

  it("captures the real method and strips any query string from the evidence path", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(
      epFetch("/platform/v1/files/upload?dir=notes", { method: "post" }),
    ).rejects.toBeInstanceOf(TypeError);
    expect(useConnection.getState().lastEvidence).toEqual({
      method: "POST",
      path: "/platform/v1/files/upload",
      kind: "TypeError",
    });
  });

  it("treats an aborted request as no evidence either way", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("gone", "AbortError")));
    await expect(epFetch("/x")).rejects.toThrow("gone");
    expect(useConnection.getState().coreDown).toBe(false);
    expect(useConnection.getState().pendingDown).toBe(false);
    expect(useConnection.getState().lastEvidence).toBeNull();
  });

  it("marks unreachable on a gateway 502/504 — nginx answered, the core did not", async () => {
    for (const status of [502, 504]) {
      reset();
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("bad gateway", { status })));
      await epFetch("/x");
      expect(useConnection.getState().coreDown, `status ${status}`).toBe(true);
      expect(useConnection.getState().lastEvidence?.kind, `status ${status}`).toBe(String(status));
    }
  });

  it("counts ANY other answer as reachable — errors and the paused 503 included", async () => {
    // A 404/500 proves epicurus answered; 503 is the *paused* state (PausedError), a
    // mood rather than an outage — it must never light the unreachable banner. Also
    // clears the #791 pending/confirmed pair and the evidence, not just coreDown.
    for (const status of [200, 404, 500, 503]) {
      useConnection.setState({
        online: true,
        coreDown: true,
        pendingDown: true,
        confirmedDown: true,
        lastEvidence: evidence("502"),
      });
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status })));
      await epFetch("/x");
      expect(useConnection.getState().coreDown, `status ${status}`).toBe(false);
      expect(useConnection.getState().pendingDown, `status ${status}`).toBe(false);
      expect(useConnection.getState().confirmedDown, `status ${status}`).toBe(false);
      expect(useConnection.getState().lastEvidence, `status ${status}`).toBeNull();
    }
  });
});

// The debounce state machine (#791) in isolation — no rendering, just the store. Fake
// timers drive the grace window deterministically instead of a real 5s wait.
describe("connection debounce state machine (#791)", () => {
  it("arms pendingDown (and coreDown, unchanged) on a first failure without confirming", () => {
    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    expect(useConnection.getState()).toMatchObject({
      coreDown: true,
      pendingDown: true,
      confirmedDown: false,
    });
  });

  it("stays unconfirmed through the grace window minus one tick", () => {
    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    act(() => vi.advanceTimersByTime(UNREACHABLE_GRACE_MS - 1));
    expect(useConnection.getState().confirmedDown).toBe(false);
  });

  it("confirms once the grace window fully elapses with no second failure", () => {
    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    act(() => vi.advanceTimersByTime(UNREACHABLE_GRACE_MS));
    expect(useConnection.getState()).toMatchObject({ pendingDown: false, confirmedDown: true });
  });

  it("confirms immediately on a second failure, without waiting out the grace", () => {
    act(() => useConnection.getState().reportUnreachable(evidence("TypeError")));
    act(() => vi.advanceTimersByTime(500)); // well under the grace window
    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    expect(useConnection.getState()).toMatchObject({
      pendingDown: false,
      confirmedDown: true,
      lastEvidence: evidence("502"),
    });
  });

  it("a healthy response before the grace window elapses clears pending without ever confirming", () => {
    act(() => useConnection.getState().reportUnreachable(evidence("504")));
    act(() => vi.advanceTimersByTime(UNREACHABLE_GRACE_MS - 1000));
    act(() => useConnection.getState().reportReachable());
    // Past the original deadline now — if the timer had leaked, this would confirm it.
    act(() => vi.advanceTimersByTime(2000));
    expect(useConnection.getState()).toMatchObject({
      coreDown: false,
      pendingDown: false,
      confirmedDown: false,
      lastEvidence: null,
    });
  });

  it("keeps lastEvidence pointed at the freshest report once confirmed", () => {
    const first = evidence("TypeError", "/platform/v1/power");
    const second = evidence("502", "/platform/v1/modules");
    const third = evidence("504", "/platform/v1/agent/send");
    act(() => useConnection.getState().reportUnreachable(first));
    act(() => useConnection.getState().reportUnreachable(second));
    act(() => useConnection.getState().reportUnreachable(third));
    expect(useConnection.getState().lastEvidence).toEqual(third);
  });

  it("a cancelled grace timer never fires late and flips confirmedDown on its own", () => {
    act(() => useConnection.getState().reportUnreachable(evidence("502"))); // t=0, deadline 5000
    act(() => vi.advanceTimersByTime(1000)); // t=1000
    act(() => useConnection.getState().reportReachable()); // cancels the t=5000 timer

    act(() => useConnection.getState().reportUnreachable(evidence("504"))); // t=1000, deadline 6000
    act(() => vi.advanceTimersByTime(4001)); // t=5001 — past the cancelled timer's old deadline
    expect(useConnection.getState().confirmedDown).toBe(false);

    act(() => vi.advanceTimersByTime(999)); // t=6000 — the fresh arm's real deadline
    expect(useConnection.getState().confirmedDown).toBe(true);
    expect(useConnection.getState().lastEvidence).toEqual(evidence("504"));
  });
});

describe("useConnectionWatch (#494)", () => {
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

  it("fires one confirming re-probe the instant a failure arms pendingDown (#791)", () => {
    const refetchVitals = vi.fn();
    renderHook(() => useConnectionWatch({ refetchVitals, onRecovered: vi.fn() }));

    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    expect(refetchVitals).toHaveBeenCalledTimes(1);

    // A second failure while still pending confirms it (see the state-machine block
    // above) but must not re-fire the probe — it already asked once for this arm.
    act(() => useConnection.getState().reportUnreachable(evidence("504")));
    expect(refetchVitals).toHaveBeenCalledTimes(1);
  });

  it("fires a fresh confirming re-probe for a new arm once the last one recovered", () => {
    const refetchVitals = vi.fn();
    renderHook(() => useConnectionWatch({ refetchVitals, onRecovered: vi.fn() }));

    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    act(() => useConnection.getState().reportReachable());
    expect(refetchVitals).toHaveBeenCalledTimes(1);

    act(() => useConnection.getState().reportUnreachable(evidence("504")));
    expect(refetchVitals).toHaveBeenCalledTimes(2);
  });

  it("re-checks immediately when the tab becomes visible while unreachable", () => {
    const refetchVitals = vi.fn();
    renderHook(() => useConnectionWatch({ refetchVitals, onRecovered: vi.fn() }));
    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    refetchVitals.mockClear(); // drop the #791 confirming re-probe counted just above

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

    act(() => useConnection.getState().reportUnreachable(evidence("502")));
    expect(onRecovered).not.toHaveBeenCalled();

    act(() => useConnection.getState().reportReachable());
    expect(onRecovered).toHaveBeenCalledTimes(1);

    // reportReachable fires on every healthy response — only the transition may count,
    // or recovery would invalidate the query cache in a storm.
    act(() => useConnection.getState().reportReachable());
    expect(onRecovered).toHaveBeenCalledTimes(1);
  });
});
