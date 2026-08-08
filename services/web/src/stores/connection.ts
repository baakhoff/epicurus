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
 *   the poll in hidden tabs, so a backgrounded PWA makes no extra requests). `coreDown`
 *   stays eager on purpose (unchanged by #791 below) — it is what gates the composer's
 *   Send button and the Files/Mail "connection lost" states, and none of those want to
 *   wait out a grace window before refusing an action that would just fail anyway.
 *
 * Two inputs keep the two states distinguishable on a LAN/VPN self-hosted setup (#460),
 * where "app up (SW cache), backend unreachable" is a normal state: phone off Wi-Fi →
 * offline; phone fine but the box/stack down → can't reach epicurus.
 *
 * **The banner debounces on top of that (#791).** A loaded self-hosted box (CPU-only
 * inference, Docker Desktop VM) throws transient blips — a gateway 502 while core-app is
 * momentarily pegged, a 504 past the gateway timeout, a stray network `TypeError` — that
 * are indistinguishable, evidence-wise, from a real outage. Putting a full-width banner
 * over the shell for one dropped request is the wrong trip, so the banner reads a second,
 * debounced pair of fields (`pendingDown`/`confirmedDown`) instead of `coreDown` directly:
 * a first failure only *arms* `pendingDown` (and `coreDown` flips as always, for the
 * consumers above); `confirmedDown` — what the banner actually renders on — trips only
 * once that evidence persists past a short grace window (`UNREACHABLE_GRACE_MS`) or a
 * second failure confirms it sooner. `useConnectionWatch` reacts to the arm by firing one
 * confirming re-probe immediately (the vitals refetch), so in the common case ("just a
 * blip") the second half of "grace or second failure" resolves via a healthy re-probe
 * long before the timer would. Any healthy response clears everything — `coreDown`,
 * `pendingDown`, `confirmedDown` — at once; recovery was never the flaky half of this.
 */
import { useEffect, useRef } from "react";
import { create } from "zustand";

/** The three failure shapes `epFetch` can observe — see its own docstring. */
export type UnreachableKind = "TypeError" | "502" | "504";

/** What tripped a reportUnreachable() call, kept for the banner's diagnostic tooltip and
 *  the console.debug trail (#791) — method + path (no query string, no origin) + which of
 *  the three failure shapes fired. */
export interface UnreachableEvidence {
  method: string;
  path: string;
  kind: UnreachableKind;
}

/** How long unconfirmed evidence (`pendingDown`) may stand before the banner trips on its
 *  own — long enough to ride out a loaded self-hosted box's ordinary gateway hiccups
 *  (the confirming re-probe usually resolves it well before this fires), short enough
 *  that a genuinely stopped stack still surfaces promptly (the #791 acceptance is
 *  "within ~5 s"). */
export const UNREACHABLE_GRACE_MS = 5_000;

interface Connection {
  /** The device has a network, as far as the browser can tell. */
  online: boolean;
  /** Evidence says epicurus is not answering — eager, single-strike, unchanged by #791.
   *  The composer's Send-gate and the Files/Mail "connection lost" states read this: they
   *  would rather refuse instantly than let an action through a debounce window only to
   *  have it fail anyway. */
  coreDown: boolean;
  /** #791: armed by a first, not-yet-confirmed failure; cleared by recovery or promoted
   *  to `confirmedDown`. Exposed so `useConnectionWatch` can fire the one confirming
   *  re-probe per arm — no other consumer should read it. */
  pendingDown: boolean;
  /** #791: the debounced signal the banner renders on (`ConnectionBanner`, src/App.tsx) —
   *  true only once evidence has persisted past the grace window or a second failure
   *  confirmed it. Never true while `pendingDown` is still just armed. */
  confirmedDown: boolean;
  /** The evidence behind the current pending/confirmed state, for the banner's tooltip
   *  and the console trail. Cleared on recovery. */
  lastEvidence: UnreachableEvidence | null;
  setOnline: (online: boolean) => void;
  reportUnreachable: (evidence: UnreachableEvidence) => void;
  reportReachable: () => void;
}

// Module-scoped, not store state: a setTimeout handle isn't serializable and nothing
// outside this file needs to see it. Every path that can leave `pendingDown` armed
// (confirm, recover, re-arm) clears it first, so at most one is ever live — see the
// three call sites below.
let graceTimer: ReturnType<typeof setTimeout> | undefined;

function clearGraceTimer(): void {
  if (graceTimer === undefined) return;
  clearTimeout(graceTimer);
  graceTimer = undefined;
}

export const useConnection = create<Connection>()((set, get) => ({
  online: typeof navigator === "undefined" || navigator.onLine,
  coreDown: false,
  pendingDown: false,
  confirmedDown: false,
  lastEvidence: null,
  setOnline: (online) => set({ online }),

  reportUnreachable: (evidence) => {
    const { pendingDown, confirmedDown } = get();
    console.debug(
      `[connection] ${evidence.method} ${evidence.path} → ${evidence.kind}` +
        ` (${confirmedDown ? "confirmed" : pendingDown ? "confirming" : "armed"})`,
    );
    // coreDown is the original #494 signal — every non-banner consumer still reacts to
    // the very first piece of evidence, unchanged by the debounce below.
    set({ coreDown: true, lastEvidence: evidence });

    if (confirmedDown) return; // banner already up — nothing left to arm or confirm
    if (pendingDown) {
      // A second failure before the grace window elapsed (very often the confirming
      // re-probe itself, see useConnectionWatch) — confirm now rather than making the
      // operator wait out the rest of the timer.
      clearGraceTimer();
      set({ confirmedDown: true, pendingDown: false });
      return;
    }
    // First failure: arm and start the grace clock. useConnectionWatch reacts to
    // pendingDown flipping true by firing one confirming re-probe right away, so in
    // practice this settles on that answer well before the timer below ever fires.
    set({ pendingDown: true });
    graceTimer = setTimeout(() => {
      graceTimer = undefined;
      // Only promote if still armed — a recovery in the meantime already cleared it.
      if (get().pendingDown) set({ confirmedDown: true, pendingDown: false });
    }, UNREACHABLE_GRACE_MS);
  },

  reportReachable: () => {
    clearGraceTimer();
    set({ coreDown: false, pendingDown: false, confirmedDown: false, lastEvidence: null });
  },
}));

/**
 * The shell's recovery wiring (#494) — mounted once. Event-shaped, never a new poll:
 * the browser's `online` event re-checks the vitals immediately (mirroring the chat
 * probe's own `online` listener); returning to a visible tab while unreachable
 * re-checks at once instead of waiting out the power poll; arming `pendingDown` fires
 * one immediate confirming re-probe (#791, below); and the moment evidence flips back
 * to reachable, `onRecovered` lets the caller un-stale the query cache so screens
 * showing outage-era data refetch instead of quietly staying stale.
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
  const pendingDown = useConnection((s) => s.pendingDown);

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

  // #791: the confirming re-probe. A first, unconfirmed failure re-checks immediately
  // instead of waiting out the grace window (or the next scheduled poll) — reusing this
  // same vitals refetch rather than a second probe path. Keyed on the false→true edge
  // only: `pendingDown` clearing (recovered, or promoted to confirmedDown) must not
  // re-fire it.
  useEffect(() => {
    if (pendingDown) refetchVitals();
  }, [pendingDown, refetchVitals]);

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
