import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { SseMessage } from "@/lib/sse";

// ── mock the SSE transport so a turn can pause on `ask_user` and a resume can continue it ──
// `sse` drives the POST turn; each call records (path, body) so we can assert the resume URL
// and the answer payload. The per-test `sseScript` decides what frames a turn emits.

let sseScript: (path: string, body: unknown) => AsyncGenerator<SseMessage>;
const sseCalls: { path: string; body: unknown }[] = [];

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return {
    ...actual,
    sse: (path: string, body: unknown) => {
      sseCalls.push({ path, body });
      return sseScript(path, body);
    },
  };
});

// No re-attach happens in these tests (every turn ends on a terminal frame), but the store
// imports the API — stub the two methods it touches.
vi.mock("@/lib/api", () => ({
  api: {
    activeRun: vi.fn(async () => null),
    cancelActiveRun: vi.fn(async () => ({ cancelled: true })),
  },
}));

import { api } from "@/lib/api";
import { useChat } from "@/stores/chat";

const delta = (text: string): SseMessage => ({
  event: "delta",
  data: JSON.stringify({ type: "delta", text }),
});
const toolFrame = (name: string, status: string, detail?: string): SseMessage => ({
  event: "tool",
  data: JSON.stringify({ type: "tool", tool: name, status, detail }),
});
// `ask_user` ends the stream with `awaiting_input` (no `done`) — the question + the run to resume.
const awaitingInput = (runId: string, question: string): SseMessage => ({
  event: "awaiting_input",
  data: JSON.stringify({ type: "awaiting_input", run_id: runId, question }),
});
const done = (): SseMessage => ({
  event: "done",
  data: JSON.stringify({ type: "done", turn: { content: "ok", tools_used: [], stopped: "completed" } }),
});

async function* nothing(): AsyncGenerator<SseMessage> {
  // default: a turn that ends with no terminal frame (unused by most cases)
}

beforeEach(() => {
  sseCalls.length = 0;
  sseScript = nothing;
  (api.activeRun as Mock).mockReset().mockResolvedValue(null);
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
    awaiting: null,
  });
  localStorage.clear();
});

describe("ask_user pause (ADR-0053, #360)", () => {
  it("pauses on awaiting_input — surfaces the question, keeps the partial turn, stops the spinner", async () => {
    // The turn emits the ask_user step, then ends the stream awaiting an answer.
    sseScript = async function* () {
      yield toolFrame("ask_user", "ok", "Which file did you mean?");
      yield awaitingInput("run-7", "Which file did you mean?");
    };

    const onDone = vi.fn(async () => {});
    await useChat.getState().send("rename it", null, onDone);

    const s = useChat.getState();
    expect(s.awaiting).toEqual({ runId: "run-7", question: "Which file did you mean?" });
    expect(s.streaming).toBe(false); // not "done" — but the spinner stops while we wait
    expect(s.error).toBeNull();
    // The optimistic user echo is handed off to history (refetched), not left dangling.
    expect(s.pendingUser).toBeNull();
    expect(onDone).toHaveBeenCalled();
    // The ask_user step stays in the live turn so the question keeps its context.
    expect(s.segments.some((seg) => seg.kind === "tool" && seg.run.tool === "ask_user")).toBe(true);
  });

  it("a blank question still pauses, with a run to resume", async () => {
    sseScript = async function* () {
      yield awaitingInput("run-3", "");
    };
    await useChat.getState().send("hmm", null, async () => {});
    expect(useChat.getState().awaiting).toEqual({ runId: "run-3", question: "" });
  });

  it("persists the pending question so a refresh keeps the prompt", async () => {
    sseScript = async function* () {
      yield awaitingInput("run-9", "Which city?");
    };
    await useChat.getState().send("what's the weather", null, async () => {});

    const stored = JSON.parse(localStorage.getItem("epicurus-chat") ?? "{}");
    expect(stored.state.awaiting).toEqual({ runId: "run-9", question: "Which city?" });
    // live turn state is still never persisted
    expect(stored.state.segments).toBeUndefined();
    expect(stored.state.streaming).toBeUndefined();
  });
});

describe("resume the answer (ADR-0053, #360)", () => {
  it("posts the answer to the suspended run and continues the turn to completion", async () => {
    sseScript = async function* () {
      yield awaitingInput("run-7", "Which file did you mean?");
    };
    await useChat.getState().send("rename it", null, async () => {});
    expect(useChat.getState().awaiting?.runId).toBe("run-7");

    // The resume stream finishes the turn.
    sseScript = async function* () {
      yield delta("renamed the readme");
      yield done();
    };
    const onResumeDone = vi.fn(async () => {});
    await useChat.getState().resume("the readme", onResumeDone);

    const resumeCall = sseCalls.at(-1)!;
    expect(resumeCall.path).toBe("/platform/v1/agent/runs/run-7/resume");
    expect(resumeCall.body).toEqual({ answer: "the readme" });
    expect(onResumeDone).toHaveBeenCalled(); // reached `done` → history refetched

    const s = useChat.getState();
    expect(s.awaiting).toBeNull();
    expect(s.streaming).toBe(false);
    expect(s.segments).toEqual([]); // the completed turn now belongs to history
  });

  it("url-encodes the run id", async () => {
    sseScript = async function* () {
      yield awaitingInput("run/awkward id", "?");
    };
    await useChat.getState().send("x", null, async () => {});
    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resume("y", async () => {});
    expect(sseCalls.at(-1)!.path).toBe("/platform/v1/agent/runs/run%2Fawkward%20id/resume");
  });

  it("is a quiet no-op when no question is pending", async () => {
    const onDone = vi.fn(async () => {});
    await useChat.getState().resume("nobody asked", onDone);
    expect(sseCalls).toHaveLength(0);
    expect(onDone).not.toHaveBeenCalled();
    expect(useChat.getState().streaming).toBe(false);
  });
});

describe("a fresh turn abandons a pending question", () => {
  it("clears a stale awaiting question when a new message is sent", async () => {
    sseScript = async function* () {
      yield awaitingInput("run-7", "Which file did you mean?");
    };
    await useChat.getState().send("rename it", null, async () => {});
    expect(useChat.getState().awaiting).not.toBeNull();

    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().send("never mind — hello", null, async () => {});
    expect(useChat.getState().awaiting).toBeNull();
  });

  it("clears the pending question on newSession and openSession", async () => {
    useChat.setState({ awaiting: { runId: "r", question: "q" } });
    useChat.getState().newSession();
    expect(useChat.getState().awaiting).toBeNull();

    useChat.setState({ awaiting: { runId: "r", question: "q" } });
    useChat.getState().openSession("other");
    expect(useChat.getState().awaiting).toBeNull();
  });
});
