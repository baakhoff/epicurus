/**
 * A live SSE feed with auto-reconnect, capped in memory — the shape both Observability
 * consoles (logs, events) use, and the one a third (automation runs) will.
 *
 * Extracted rather than copied. The reconnect loop looks trivial and is not: it has to
 * distinguish an *intentional* abort (a filter change, or unmount) from a real drop, or
 * the "reconnect" it schedules races the teardown that just cancelled it and the feed
 * quietly reconnects forever behind a closed screen. That check is one line
 * (`err.name === "AbortError"`) and it is exactly the line a second copy loses.
 */
import { useEffect, useRef, useState } from "react";

/** How long to wait before re-opening a stream that dropped or ended. */
const RECONNECT_DELAY_MS = 3_000;

export interface SseFeed<T> {
  /** Entries oldest-first, capped at `max`. */
  entries: T[];
  /** True while the stream is down and a reconnect is pending. */
  disconnected: boolean;
  /** Drop everything currently displayed (the stream keeps running). */
  clear: () => void;
}

/**
 * Subscribe to `connect` and accumulate what it yields.
 *
 * @param connect  Opens the stream. Called again on every reconnect, and whenever
 *                 `resetKey` changes — read the latest filters from the closure; the hook
 *                 always calls the newest version it was handed, so it need not be
 *                 memoized.
 * @param resetKey A string identifying the current filters. When it changes the stream is
 *                 torn down, the entries are cleared, and `connect` is called afresh —
 *                 otherwise the feed would mix results from two different filters.
 * @param max      How many entries to keep. Older ones fall off the front so a long-lived
 *                 tab cannot grow the DOM without bound.
 */
export function useSseFeed<T>(
  connect: (signal: AbortSignal) => AsyncGenerator<T>,
  resetKey: string,
  max: number,
): SseFeed<T> {
  const [entries, setEntries] = useState<T[]>([]);
  const [disconnected, setDisconnected] = useState(false);

  // The subscription effect keys on resetKey alone, so it must not close over a stale
  // `connect`. A ref keeps the newest closure reachable without making the subscription
  // depend on it — `connect` is a fresh function every render, so depending on it would
  // tear down and re-open the stream on each parent render.
  //
  // The write lives in an effect rather than in the render body (react-hooks/refs): a
  // render must be side-effect free, since React may run one and discard it — and the
  // mutation would stick. Declaration order carries the correctness: this effect is
  // declared before the subscription below, so on any render that changes resetKey React
  // runs this setup first and the subscription then reads the current closure. useRef's
  // initial value covers the first run, before any effect has fired.
  const connectRef = useRef(connect);
  useEffect(() => {
    connectRef.current = connect;
  });

  // Clear on a filter change by adjusting state during render — the React-blessed
  // alternative to a setState-in-effect, and the idiom this screen already used.
  const [appliedKey, setAppliedKey] = useState(resetKey);
  if (resetKey !== appliedKey) {
    setAppliedKey(resetKey);
    setEntries([]);
  }

  useEffect(() => {
    let active = true;
    let abort: AbortController | null = null;

    async function run() {
      while (active) {
        // A fresh controller per attempt: reusing an aborted one would make every
        // reconnect fail instantly.
        abort?.abort();
        abort = new AbortController();
        setDisconnected(false);
        try {
          for await (const entry of connectRef.current(abort.signal)) {
            if (!active) break;
            setEntries((prev) => {
              const next = [...prev, entry];
              return next.length > max ? next.slice(next.length - max) : next;
            });
          }
          // The server closed the stream cleanly — reconnect after a beat.
          if (active) {
            setDisconnected(true);
            await delay(RECONNECT_DELAY_MS);
          }
        } catch (err: unknown) {
          if (!active) break;
          // We aborted on purpose (filter change / unmount): stop, do not reconnect.
          if (err instanceof Error && err.name === "AbortError") break;
          setDisconnected(true);
          await delay(RECONNECT_DELAY_MS);
        }
      }
    }

    run().catch(() => {});

    return () => {
      active = false;
      abort?.abort();
    };
  }, [resetKey, max]);

  return {
    entries,
    disconnected,
    clear: () => setEntries([]),
  };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
