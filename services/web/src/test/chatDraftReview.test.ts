import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { SseMessage } from "@/lib/sse";

// ── mock the SSE transport so a turn can pause on a draft review and a decision can continue it ──
// Mirrors the ask_user harness: `sse` drives the POST turn; each call records (path, body) so we
// can assert the resolve URL + the decision payload. `sseScript` decides a turn's frames.

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
  },
}));

import { api } from "@/lib/api";
import { useChat } from "@/stores/chat";

const DRAFT = { to: "bob@x.com", subject: "Lunch?", body: "Noon works." };

// A draft-first send pauses the stream with `awaiting_input` + `awaiting_kind: "draft_review"` and
// the composed draft — no `done` (ADR-0085, #563).
const draftReview = (runId: string, draft: object): SseMessage => ({
  event: "awaiting_input",
  data: JSON.stringify({
    type: "awaiting_input",
    run_id: runId,
    awaiting_kind: "draft_review",
    draft,
  }),
});
const done = (): SseMessage => ({
  event: "done",
  data: JSON.stringify({
    type: "done",
    turn: { content: "sent", tools_used: [], stopped: "completed" },
  }),
});

async function* nothing(): AsyncGenerator<SseMessage> {}

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
    awaitingDraft: null,
  });
  localStorage.clear();
});

describe("draft-review pause (ADR-0085, #563)", () => {
  it("pauses on a draft_review frame — captures the run + parsed draft, stops the spinner", async () => {
    sseScript = async function* () {
      yield draftReview("run-7", DRAFT);
    };

    const onDone = vi.fn(async () => {});
    await useChat.getState().send("email bob about lunch", null, onDone);

    const s = useChat.getState();
    // Nullish fields (cc / reply_to_original) are absent here, so the parsed draft is just what
    // was sent — the optional keys are omitted, not null-filled.
    expect(s.awaitingDraft).toEqual({ runId: "run-7", draft: DRAFT });
    expect(s.awaiting).toBeNull(); // a draft pause is not an ask_user question
    expect(s.streaming).toBe(false); // spinner stops while we review
    expect(s.error).toBeNull();
    expect(onDone).toHaveBeenCalled();
  });

  it("persists the pending draft so a refresh keeps the review pane", async () => {
    sseScript = async function* () {
      yield draftReview("run-9", DRAFT);
    };
    await useChat.getState().send("email bob", null, async () => {});

    const stored = JSON.parse(localStorage.getItem("epicurus-chat") ?? "{}");
    expect(stored.state.awaitingDraft.runId).toBe("run-9");
    expect(stored.state.awaitingDraft.draft.subject).toBe("Lunch?");
    // live turn state is still never persisted
    expect(stored.state.segments).toBeUndefined();
    expect(stored.state.streaming).toBeUndefined();
  });
});

describe("resolve the draft (ADR-0085, #563)", () => {
  it("Confirm posts decision=send to the run and continues the turn to completion", async () => {
    sseScript = async function* () {
      yield draftReview("run-7", DRAFT);
    };
    await useChat.getState().send("email bob", null, async () => {});
    expect(useChat.getState().awaitingDraft?.runId).toBe("run-7");

    sseScript = async function* () {
      yield done();
    };
    const onResolveDone = vi.fn(async () => {});
    await useChat.getState().resolveDraft("send", onResolveDone);

    const call = sseCalls.at(-1)!;
    expect(call.path).toBe("/platform/v1/agent/runs/run-7/draft");
    expect(call.body).toEqual({ decision: "send" });
    expect(onResolveDone).toHaveBeenCalled();
    const s = useChat.getState();
    expect(s.awaitingDraft).toBeNull();
    expect(s.streaming).toBe(false);
  });

  it("Decline posts decision=decline and carries a trimmed reason", async () => {
    sseScript = async function* () {
      yield draftReview("run-7", DRAFT);
    };
    await useChat.getState().send("email bob", null, async () => {});

    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resolveDraft("decline", async () => {}, "  wrong recipient  ");
    const call = sseCalls.at(-1)!;
    expect(call.path).toBe("/platform/v1/agent/runs/run-7/draft");
    expect(call.body).toEqual({ decision: "decline", reason: "wrong recipient" });
  });

  it("omits a blank reason rather than sending it", async () => {
    sseScript = async function* () {
      yield draftReview("run-7", DRAFT);
    };
    await useChat.getState().send("email bob", null, async () => {});
    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resolveDraft("decline", async () => {}, "   ");
    expect(sseCalls.at(-1)!.body).toEqual({ decision: "decline" });
  });

  it("url-encodes the run id", async () => {
    sseScript = async function* () {
      yield draftReview("run/awkward id", DRAFT);
    };
    await useChat.getState().send("x", null, async () => {});
    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().resolveDraft("send", async () => {});
    expect(sseCalls.at(-1)!.path).toBe("/platform/v1/agent/runs/run%2Fawkward%20id/draft");
  });

  it("is a quiet no-op when no draft is pending", async () => {
    const onDone = vi.fn(async () => {});
    await useChat.getState().resolveDraft("send", onDone);
    expect(sseCalls).toHaveLength(0);
    expect(onDone).not.toHaveBeenCalled();
    expect(useChat.getState().streaming).toBe(false);
  });
});

describe("a fresh turn abandons a pending draft", () => {
  it("clears a stale awaitingDraft when a new message is sent", async () => {
    sseScript = async function* () {
      yield draftReview("run-7", DRAFT);
    };
    await useChat.getState().send("email bob", null, async () => {});
    expect(useChat.getState().awaitingDraft).not.toBeNull();

    sseScript = async function* () {
      yield done();
    };
    await useChat.getState().send("never mind — hello", null, async () => {});
    expect(useChat.getState().awaitingDraft).toBeNull();
  });

  it("clears the pending draft on newSession and openSession", () => {
    useChat.setState({ awaitingDraft: { runId: "r", draft: { to: "", subject: "", body: "" } } });
    useChat.getState().newSession();
    expect(useChat.getState().awaitingDraft).toBeNull();

    useChat.setState({ awaitingDraft: { runId: "r", draft: { to: "", subject: "", body: "" } } });
    useChat.getState().openSession("other");
    expect(useChat.getState().awaitingDraft).toBeNull();
  });
});
