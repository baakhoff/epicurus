import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

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
