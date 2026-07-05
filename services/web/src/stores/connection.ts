/**
 * Shell-wide connection state (#494): ONE signal, two inputs.
 *
 * - `online` mirrors the browser's own judgement (`navigator.onLine` + events): the
 *   device has no network at all.
 * - `coreDown` is evidence-based: every /platform request the app already makes doubles
 *   as the reachability probe (`epFetch`, src/lib/http.ts) — a network-level failure or
 *   a gateway 502/504 marks epicurus unreachable, ANY other answer marks it reachable.
 *   There is no dedicated polling endpoint: the PowerOrb's existing 15 s `power` poll is
 *   the heartbeat that trips and clears this while the tab is visible (TanStack pauses
 *   the poll in hidden tabs, so a backgrounded PWA makes no extra requests).
 *
 * Two inputs keep the two states distinguishable on a LAN/VPN self-hosted setup (#460),
 * where "app up (SW cache), backend unreachable" is a normal state: phone off Wi-Fi →
 * offline; phone fine but the box/stack down → can't reach epicurus.
 */
import { useEffect, useRef } from "react";
import { create } from "zustand";

interface Connection {
  /** The device has a network, as far as the browser can tell. */
  online: boolean;
  /** Evidence says epicurus is not answering (network failure or gateway 502/504). */
  coreDown: boolean;
  setOnline: (online: boolean) => void;
  reportUnreachable: () => void;
  reportReachable: () => void;
}

export const useConnection = create<Connection>()((set) => ({
  online: typeof navigator === "undefined" || navigator.onLine,
  coreDown: false,
  setOnline: (online) => set({ online }),
  reportUnreachable: () => set({ coreDown: true }),
  reportReachable: () => set({ coreDown: false }),
}));

/**
 * The shell's recovery wiring (#494) — mounted once. Event-shaped, never a new poll:
 * the browser's `online` event re-checks the vitals immediately (mirroring the chat
 * probe's own `online` listener); returning to a visible tab while unreachable
 * re-checks at once instead of waiting out the power poll; and the moment evidence
 * flips back to reachable, `onRecovered` lets the caller un-stale the query cache so
 * screens showing outage-era data refetch instead of quietly staying stale.
 */
export function useConnectionWatch({
  refetchVitals,
  onRecovered,
}: {
  /** Re-check the always-on queries (power, modules) right now. */
  refetchVitals: () => void;
  /** Called exactly once per outage, when evidence flips back to reachable. */
  onRecovered: () => void;
}): void {
  const coreDown = useConnection((s) => s.coreDown);

  useEffect(() => {
    const on = () => {
      useConnection.getState().setOnline(true);
      refetchVitals();
    };
    const off = () => useConnection.getState().setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, [refetchVitals]);

  useEffect(() => {
    if (!coreDown) return;
    const onVisible = () => {
      if (document.visibilityState === "visible") refetchVitals();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [coreDown, refetchVitals]);

  // Only the down→up *transition* fires — reportReachable is called on every healthy
  // response, and re-invalidating the cache on each would be a refetch storm.
  const wasDown = useRef(false);
  useEffect(() => {
    if (wasDown.current && !coreDown) onRecovered();
    wasDown.current = coreDown;
  }, [coreDown, onRecovered]);
}
