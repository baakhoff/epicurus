import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { SseMessage } from "@/lib/sse";

// ── mock the SSE transport + API so we can script a drop and the re-attach ─────

// Per-test scripts the mocked transport plays. `sse` drives a fresh POST turn; `sseRequest`
// drives a GET re-attach (and records the URL so we can assert the after_seq offset).
let sseScript: () => AsyncGenerator<SseMessage>;
let sseRequestScript: (path: string) => AsyncGenerator<SseMessage>;
let lastReattachPath: string | undefined;

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return {
    ...actual,
    sse: () => sseScript(),
    sseRequest: (path: string) => {
      lastReattachPath = path;
      return sseRequestScript(path);
    },
  };
});

vi.mock("@/lib/api", () => ({
  api: {
    activeRun: vi.fn(),
    cancelActiveRun: vi.fn(async () => ({ cancelled: true })),
  },
}));

import { api } from "@/lib/api";
import { useChat } from "@/stores/chat";

const delta = (text: string, id?: string): SseMessage => ({
  event: "delta",
  data: JSON.stringify({ type: "delta", text }),
  id,
});
const done = (id?: string): SseMessage => ({
  event: "done",
  data: JSON.stringify({ type: "done", turn: { content: "hi", tools_used: [], stopped: "completed" } }),
  id,
});
const gone = (): SseMessage => ({
  event: "gone",
  data: JSON.stringify({ type: "gone", detail: "run not found" }),
});

async function* nothing(): AsyncGenerator<SseMessage> {
  // a stream that ends with no terminal frame == a dropped connection
}

beforeEach(() => {
  lastReattachPath = undefined;
  sseScript = nothing;
  sseRequestScript = nothing;
  (api.activeRun as Mock).mockReset();
  (api.cancelActiveRun as Mock).mockReset().mockResolvedValue({ cancelled: true });
  useChat.setState({
    sessionId: "test-session",
    draft: "",
    pendingUser: null,
    pendingAttachments: [],
    segments: [],
    streaming: false,
    readiness: null,
    error: null,
    paused: false,
    abort: null,
    lastSeq: 0,
  });
  localStorage.clear();
});

describe("session persistence (#376)", () => {
  it("persists only sessionId + draft, never live turn state", () => {
    useChat.setState({
      sessionId: "sess-xyz",
      draft: "keep me",
      segments: [{ kind: "text", text: "live answer" }],
      streaming: true,
    });
    const stored = JSON.parse(localStorage.getItem("epicurus-chat") ?? "{}");
    expect(stored.state.sessionId).toBe("sess-xyz");
    expect(stored.state.draft).toBe("keep me");
    expect(stored.state.segments).toBeUndefined();
    expect(stored.state.streaming).toBeUndefined();
  });
});

describe("re-attach on a dropped stream (#376)", () => {
  it("re-attaches to the running turn and finishes it, from the dropped offset", async () => {
    // The POST stream delivers one token then drops (no terminal frame)…
    sseScript = async function* () {
      yield delta("par", "1");
    };
    // …the turn is still running server-side, so re-attach picks it up and completes it.
    (api.activeRun as Mock).mockResolvedValue({ run_id: "r1", last_seq: 1 });
    sseRequestScript = async function* () {
      yield delta("tial", "2");
      yield done("3");
    };

    const onDone = vi.fn(async () => {});
    await useChat.getState().send("hi", null, onDone);

    expect(api.activeRun).toHaveBeenCalledWith("test-session");
    expect(lastReattachPath).toContain("/runs/r1/stream");
    expect(lastReattachPath).toContain("after_seq=1"); // resumed from the last seq we saw
    expect(onDone).toHaveBeenCalled(); // the turn reached `done`
    expect(useChat.getState().streaming).toBe(false);
    expect(useChat.getState().error).toBeNull();
    expect(useChat.getState().segments).toEqual([]); // cleared once the server owns the turn
  });

  it("falls back to history when the run is already gone", async () => {
    sseScript = async function* () {
      yield delta("x", "1");
    };
    (api.activeRun as Mock).mockResolvedValue({ run_id: "r1", last_seq: 1 });
    sseRequestScript = async function* () {
      yield gone();
    };

    const onDone = vi.fn(async () => {});
    await useChat.getState().send("hi", null, onDone);

    expect(onDone).toHaveBeenCalled(); // gone → refetch history (the answer is durable)
    expect(useChat.getState().streaming).toBe(false);
  });
});

describe("resumeIfActive (#376)", () => {
  it("re-attaches when the session has an in-flight run", async () => {
    (api.activeRun as Mock).mockResolvedValue({ run_id: "r9", last_seq: 0 });
    sseRequestScript = async function* () {
      yield delta("hello", "1");
      yield done("2");
    };

    const onDone = vi.fn(async () => {});
    await useChat.getState().resumeIfActive(onDone);

    expect(lastReattachPath).toContain("/runs/r9/stream");
    expect(onDone).toHaveBeenCalled();
    expect(useChat.getState().streaming).toBe(false);
  });

  it("is a quiet no-op when there is no in-flight run", async () => {
    (api.activeRun as Mock).mockResolvedValue(null);
    const onDone = vi.fn(async () => {});
    await useChat.getState().resumeIfActive(onDone);
    // Idle + nothing running → don't churn history or touch the spinner.
    expect(onDone).not.toHaveBeenCalled();
    expect(useChat.getState().streaming).toBe(false);
  });

  it("does not open a second stream while one is already live", async () => {
    useChat.setState({ streaming: true, abort: new AbortController() });
    const onDone = vi.fn(async () => {});
    await useChat.getState().resumeIfActive(onDone);
    expect(api.activeRun).not.toHaveBeenCalled();
  });
});

describe("stop cancels the detached turn server-side (#376)", () => {
  it("aborts locally and asks the server to cancel", () => {
    useChat.setState({ abort: new AbortController(), streaming: true });
    useChat.getState().stop();
    expect(api.cancelActiveRun).toHaveBeenCalledWith("test-session");
  });
});

// ── reattach exhaustion: probe vs recovery (#477) ───────────────────────────────
//
// A reattach loop that fails every attempt (server unreachable the whole time) must only
// surface the "lost connection" banner when there was a real turn to lose — never for an
// idle probe that never confirmed one. These drive the loop through its full ~23.5s backoff
// budget (500+1000+2000+4000+8000+8000ms across 6 attempts, mirroring MAX_REATTACH_ATTEMPTS
// and backoffMs in chat.ts), so they fake timers.
const MAX_REATTACH_ATTEMPTS_UNDER_TEST = 6;

describe("reattach exhaustion classifies probe vs recovery (#477)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("a probe that never confirms a run gives up silently — no banner", async () => {
    // Every attempt fails to even reach the server — classic flaky-mobile-radio case.
    (api.activeRun as Mock).mockRejectedValue(new Error("network unreachable"));
    const onDone = vi.fn(async () => {});

    const resumed = useChat.getState().resumeIfActive(onDone);
    await vi.advanceTimersByTimeAsync(30_000); // covers all 6 backoff sleeps
    await resumed;

    expect(useChat.getState().error).toBeNull();
    expect(useChat.getState().reconnectable).toBe(false);
    expect(useChat.getState().streaming).toBe(false);
    expect(onDone).not.toHaveBeenCalled(); // nothing to reconcile — history was never touched
  });

  it("a probe re-arms for free on the next call after a silent give-up", async () => {
    (api.activeRun as Mock).mockRejectedValue(new Error("network unreachable"));
    const first = useChat.getState().resumeIfActive(vi.fn(async () => {}));
    await vi.advanceTimersByTimeAsync(30_000);
    await first;
    const callsSoFar = (api.activeRun as Mock).mock.calls.length;
    expect(callsSoFar).toBe(MAX_REATTACH_ATTEMPTS_UNDER_TEST);

    // A brand-new probe (e.g. the next visibilitychange) gets a fresh 6-attempt budget —
    // there's no lingering "gave up permanently" state to re-arm.
    const second = useChat.getState().resumeIfActive(vi.fn(async () => {}));
    await vi.advanceTimersByTimeAsync(30_000);
    await second;
    expect((api.activeRun as Mock).mock.calls.length).toBe(2 * MAX_REATTACH_ATTEMPTS_UNDER_TEST);
  });

  it("a confirmed recovery (409 on send) surfaces the banner on exhaustion", async () => {
    // The initial POST reports a turn already running for this session (409); every
    // subsequent activeRun check then fails to reach the server. No `yield` needed — this
    // never actually iterates as a generator, it throws before `sse()` returns one.
    sseScript = () => {
      throw Object.assign(new Error("Conflict"), { status: 409 });
    };
    (api.activeRun as Mock).mockRejectedValue(new Error("network unreachable"));
    const onDone = vi.fn(async () => {});

    const sent = useChat.getState().send("hi", null, onDone);
    await vi.advanceTimersByTimeAsync(30_000);
    await sent;

    expect(useChat.getState().error).toBe("lost connection to the running turn");
    expect(useChat.getState().reconnectable).toBe(true);
  });

  it("a probe that finds a run, then loses it, still surfaces the banner (recovery semantics)", async () => {
    // Attempt 1 confirms a real run and attaches — but the stream drops immediately (no
    // terminal frame), and every attempt after that can't even reach the server again.
    let calls = 0;
    (api.activeRun as Mock).mockImplementation(async () => {
      calls += 1;
      if (calls === 1) return { run_id: "r1", last_seq: 0 };
      throw new Error("network unreachable");
    });
    sseRequestScript = async function* () {
      // ends with no terminal frame == dropped
    };
    const onDone = vi.fn(async () => {});

    const resumed = useChat.getState().resumeIfActive(onDone);
    await vi.advanceTimersByTimeAsync(30_000);
    await resumed;

    // This is the case a naive `mode === "recovery"` check would get wrong: the loop was
    // *entered* as a probe, but finding a real run partway through makes its exhaustion a
    // genuine recovery failure — the user has real state to reconcile with.
    expect(useChat.getState().error).toBe("lost connection to the running turn");
    expect(useChat.getState().reconnectable).toBe(true);
  });

  it("an online signal arriving mid-backoff resets the attempt budget instead of counting against it", async () => {
    (api.activeRun as Mock).mockRejectedValue(new Error("network unreachable"));
    const onDone = vi.fn(async () => {});

    const resumed = useChat.getState().resumeIfActive(onDone);
    // Attempt 0 calls immediately (no timer needed); advancing past its 500ms backoff lets
    // attempt 1 fire its own call, landing it mid-sleep on attempt 1's 1000ms backoff —
    // that's where the browser's `online` event lands.
    await vi.advanceTimersByTimeAsync(500);
    expect((api.activeRun as Mock).mock.calls.length).toBe(2);
    void useChat.getState().resumeIfActive(vi.fn(async () => {}), /* isConnectivitySignal */ true);

    await vi.advanceTimersByTimeAsync(60_000); // enough for a full fresh 6-attempt budget
    await resumed;

    // Without the reset, exhaustion would land at exactly 6 total calls. With it, the
    // in-flight loop got a full fresh budget on top of the 2 it had already spent.
    expect((api.activeRun as Mock).mock.calls.length).toBeGreaterThan(MAX_REATTACH_ATTEMPTS_UNDER_TEST);
  });
});
