import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { SseMessage } from "@/lib/sse";

// ── mock the SSE transport so a turn can pause on ask_approval and a decision can continue it ──
// Mirrors the ask_user/draft-review harnesses: `sse` drives the POST turn; each call records
// (path, body) so we can assert the resolve URL + the decision payload. `sseScript` decides a
// turn's frames. Unlike those two, resolving an approval also calls the owning module's own
// review-decision endpoint directly (`api.approveSuggestion`/`rejectSuggestion`) — mocked here
// so a test can assert both the module call *and* the resume call it's followed by.

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

vi.mock("@/lib/api", () => ({
  api: {
    activeRun: vi.fn(async () => null),
    cancelActiveRun: vi.fn(async () => ({ cancelled: true })),
    approveSuggestion: vi.fn(async () => ({})),
    rejectSuggestion: vi.fn(async () => ({})),
  },
}));

import { api } from "@/lib/api";
import { useChat } from "@/stores/chat";

const REF = { ref_id: "sugg-1", module: "knowledge", kind: "suggestion", title: "Update goals.md" };

// ask_approval ends the stream with `awaiting_input` + `awaiting_kind: "approval"` and a summary
// + refs (possibly empty) — no `done` (#745, ADR-0117).
const approvalPause = (runId: string, summary: string, refs: object[] = []): SseMessage => ({
  event: "awaiting_input",
  data: JSON.stringify({
    type: "awaiting_input",
    run_id: runId,
    awaiting_kind: "approval",
    summary,
    refs,
  }),
});
const done = (): SseMessage => ({
  event: "done",
  data: JSON.stringify({
    type: "done",
    turn: { content: "done", tools_used: [], stopped: "completed" },
  }),
});

async function* nothing(): AsyncGenerator<SseMessage> {}

beforeEach(() => {
  sseCalls.length = 0;
  sseScript = nothing;
  (api.activeRun as Mock).mockReset().mockResolvedValue(null);
  (api.cancelActiveRun as Mock).mockReset().mockResolvedValue({ cancelled: true });
  (api.approveSuggestion as Mock).mockReset().mockResolvedValue({});
  (api.rejectSuggestion as Mock).mockReset().mockResolvedValue({});
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
    awaitingDraft: null,
    awaitingApproval: null,
  });
  localStorage.clear();
});

describe("ask_approval pause (#745, ADR-0117)", () => {
  it("pauses on an approval frame — captures the run, summary, and refs, stops the spinner", async () => {
    sseScript = async function* () {
      yield approvalPause("run-7", "Update goals.md to add a Q3 milestone", [REF]);
    };

    const onDone = vi.fn(async () => {});
    await useChat.getState().send("update my goals note", null, onDone);

    const s = useChat.getState();
    expect(s.awaitingApproval).toEqual({
      runId: "run-7",
      summary: "Update goals.md to add a Q3 milestone",
      refs: [REF],
    });
    expect(s.awaiting).toBeNull(); // an approval pause is not an ask_user question
    expect(s.awaitingDraft).toBeNull();
    expect(s.streaming).toBe(false);
    expect(s.error).toBeNull();
    expect(onDone).toHaveBeenCalled();
  });

  it("a blank summary with no refs still pauses", async () => {
    sseScript = async function* () {
      yield approvalPause("run-3", "");
    };
    await useChat.getState().send("go", null, async () => {});
    expect(useChat.getState().awaitingApproval).toEqual({ runId: "run-3", summary: "", refs: [] });
  });

  it("persists the pending approval so a refresh keeps the card", async () => {
    sseScript = async function* () {
      yield approvalPause("run-9", "Update goals.md", [REF]);
    };
    await useChat.getState().send("update it", null, async () => {});

    const stored = JSON.parse(localStorage.getItem("epicurus-chat") ?? "{}");
    expect(stored.state.awaitingApproval).toEqual({
      runId: "run-9",
      summary: "Update goals.md",
      refs: [REF],
    });
    expect(stored.state.segments).toBeUndefined();
    expect(stored.state.streaming).toBeUndefined();
  });
});

describe("resolve the approval (#745, ADR-0117)", () => {
  it("Approve calls the module's approve endpoint per ref, then posts decision=approved and continues", async () => {
    sseScript = async function* () {
      yield approvalPause("run-7", "Update goals.md", [REF]);
    };
    await useChat.getState().send("update it", null, async () => {});

    sseScript = async function* () {
      yield done();
    };
    const onResolveDone = vi.fn(async () => {});
    await useChat.getState().resolveApproval("approved", onResolveDone);

    expect(api.approveSuggestion).toHaveBeenCalledWith("knowledge", "review", "sugg-1");
    expect(api.rejectSuggestion).not.toHaveBeenCalled();
    const call = sseCalls.at(-1)!;
    expect(call.path).toBe("/platform/v1/agent/runs/run-7/approval");
    expect(call.body).toEqual({ decision: "approved" });
    expect(onResolveDone).toHaveBeenCalled();
    const s = useChat.getState();
    expect(s.awaitingApproval).toBeNull();
    expect(s.streaming).toBe(false);
  });

  it("Reject calls the module's reject endpoint per ref, then posts decision=rejected", async () => {
    sseScript = async function* () {
      yield approvalPause("run-7", "Update goals.md", [REF]);
    };
    await useChat.getState().send("update it", null, async () => {});

    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resolveApproval("rejected", async () => {});

    expect(api.rejectSuggestion).toHaveBeenCalledWith("knowledge", "review", "sugg-1");
    expect(api.approveSuggestion).not.toHaveBeenCalled();
    expect(sseCalls.at(-1)!.body).toEqual({ decision: "rejected" });
  });

  it("carries a trimmed comment when given one", async () => {
    sseScript = async function* () {
      yield approvalPause("run-7", "Update goals.md", [REF]);
    };
    await useChat.getState().send("update it", null, async () => {});
    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resolveApproval("rejected", async () => {}, "  looks wrong  ");
    expect(sseCalls.at(-1)!.body).toEqual({ decision: "rejected", comment: "looks wrong" });
  });

  it("with no refs, skips the module call entirely and just resumes", async () => {
    sseScript = async function* () {
      yield approvalPause("run-7", "Nothing to link", []);
    };
    await useChat.getState().send("go", null, async () => {});
    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resolveApproval("approved", async () => {});
    expect(api.approveSuggestion).not.toHaveBeenCalled();
    expect(api.rejectSuggestion).not.toHaveBeenCalled();
    expect(sseCalls.at(-1)!.body).toEqual({ decision: "approved" });
  });

  it("a failed module call doesn't block the resume — it's told to the model as a comment", async () => {
    sseScript = async function* () {
      yield approvalPause("run-7", "Update goals.md", [REF]);
    };
    await useChat.getState().send("update it", null, async () => {});

    (api.approveSuggestion as Mock).mockRejectedValueOnce(new Error("403 Forbidden"));
    sseScript = async function* () {
      yield done();
    };
    const onResolveDone = vi.fn(async () => {});
    await useChat.getState().resolveApproval("approved", onResolveDone);

    // The turn still resumes (the operator's decision isn't lost)...
    expect(onResolveDone).toHaveBeenCalled();
    const call = sseCalls.at(-1)!;
    expect(call.path).toBe("/platform/v1/agent/runs/run-7/approval");
    const body = call.body as { decision: string; comment?: string };
    expect(body.decision).toBe("approved");
    // ...but the model is told the module side may not have actually updated, not left to
    // assume the change was applied.
    expect(body.comment).toContain("Update goals.md");
    expect(body.comment).toContain("403 Forbidden");
  });

  it("url-encodes the run id", async () => {
    sseScript = async function* () {
      yield approvalPause("run/awkward id", "x", []);
    };
    await useChat.getState().send("go", null, async () => {});
    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resolveApproval("approved", async () => {});
    expect(sseCalls.at(-1)!.path).toBe("/platform/v1/agent/runs/run%2Fawkward%20id/approval");
  });

  it("is a quiet no-op when nothing is pending", async () => {
    const onDone = vi.fn(async () => {});
    await useChat.getState().resolveApproval("approved", onDone);
    expect(sseCalls).toHaveLength(0);
    expect(api.approveSuggestion).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("is a no-op while a stream is already live", async () => {
    useChat.setState({
      awaitingApproval: { runId: "run-7", summary: "x", refs: [REF] },
      streaming: true,
    });
    const onDone = vi.fn(async () => {});
    await useChat.getState().resolveApproval("approved", onDone);
    expect(sseCalls).toHaveLength(0);
    expect(api.approveSuggestion).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });
});

describe("a fresh turn abandons a pending approval", () => {
  it("clears a stale awaitingApproval when a new message is sent", async () => {
    sseScript = async function* () {
      yield approvalPause("run-7", "Update goals.md", [REF]);
    };
    await useChat.getState().send("update it", null, async () => {});
    expect(useChat.getState().awaitingApproval).not.toBeNull();

    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().send("never mind — hello", null, async () => {});
    expect(useChat.getState().awaitingApproval).toBeNull();
  });

  it("clears the pending approval on newSession and openSession", async () => {
    useChat.setState({ awaitingApproval: { runId: "r", summary: "s", refs: [] } });
    useChat.getState().newSession();
    expect(useChat.getState().awaitingApproval).toBeNull();

    useChat.setState({ awaitingApproval: { runId: "r", summary: "s", refs: [] } });
    useChat.getState().openSession("other");
    expect(useChat.getState().awaitingApproval).toBeNull();
  });
});
