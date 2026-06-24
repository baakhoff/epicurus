import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Attachment } from "@/lib/contracts";
import type { SseMessage } from "@/lib/sse";

// ── mock the SSE transport so send() drives off a scripted stream ──────────────

const sentBodies: unknown[] = [];

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return {
    ...actual,
    sse: async function* (_path: string, body: unknown): AsyncGenerator<SseMessage> {
      sentBodies.push(body);
      // A readiness frame for the turn's model, then an immediate done. The data is a full
      // AgentEvent (type "readiness") carrying the snapshot — what the store parses.
      yield {
        event: "readiness",
        data: JSON.stringify({
          type: "readiness",
          readiness: {
            ready: false,
            power: "idle",
            components: [{ name: "model", ready: false, detail: "qwen2.5:7b · warming" }],
          },
        }),
      };
      yield {
        event: "done",
        data: JSON.stringify({ type: "done", turn: { content: "hi", tools_used: [], stopped: "completed" } }),
      };
    },
  };
});

import { useChat } from "@/stores/chat";

beforeEach(() => {
  sentBodies.length = 0;
  useChat.getState().newSession();
  // newSession deliberately preserves the unsent draft (see the draft tests below),
  // so reset it explicitly between cases to keep them independent.
  useChat.setState({ draft: "", streaming: false, abort: null });
});

describe("chat draft", () => {
  it("holds the unsent draft in the store", () => {
    useChat.getState().setDraft("a half-typed thought");
    expect(useChat.getState().draft).toBe("a half-typed thought");
  });

  // The fix: the draft lives in the store, not in the screen's local state, so it
  // outlives the ChatScreen unmount that happens when you navigate away and back.
  // New-chat / open-session (the other ways the screen re-renders) must not wipe it.
  it("keeps the draft across new-session and open-session", () => {
    useChat.getState().setDraft("survives navigation");
    useChat.getState().newSession();
    expect(useChat.getState().draft).toBe("survives navigation");
    useChat.getState().openSession("another-session");
    expect(useChat.getState().draft).toBe("survives navigation");
  });

  it("clears the draft once the message is sent", async () => {
    useChat.getState().setDraft("sending now");
    await useChat.getState().send("sending now", null, async () => {});
    expect(useChat.getState().draft).toBe("");
  });
});

describe("chat send → the selected model drives the turn", () => {
  it("includes the chat-selected model in the streamed request body", async () => {
    await useChat.getState().send("hello", "qwen2.5:7b", async () => {});
    expect(sentBodies).toHaveLength(1);
    const body = sentBodies[0] as { model?: string; messages: { content: string }[] };
    // The per-session model override is what the turn (and its readiness) runs on,
    // even if the global default differs.
    expect(body.model).toBe("qwen2.5:7b");
    expect(body.messages[0].content).toBe("hello");
  });

  it("omits the model when none is selected (core uses its default)", async () => {
    await useChat.getState().send("hello", null, async () => {});
    const body = sentBodies[0] as { model?: string };
    expect(body.model).toBeUndefined();
  });

  it("surfaces the readiness frame the stream leads with (model-aware warming)", async () => {
    // Capture the readiness mid-stream: it names the *selected* model, proving the warming
    // bar reflects the model the turn will actually use.
    const seen: string[] = [];
    const unsub = useChat.subscribe((s) => {
      const detail = s.readiness?.components.find((c) => c.name === "model")?.detail;
      if (detail) seen.push(detail);
    });
    await useChat.getState().send("hello", "qwen2.5:7b", async () => {});
    unsub();
    expect(seen.some((d) => d.includes("qwen2.5:7b"))).toBe(true);
  });
});

describe("chat send → optimistic attachments", () => {
  const ATT: Attachment = { att_id: "att-1", source: "chat", kind: "file", title: "report.pdf" };

  // The bug: attachments were sent to the server but never stored on the optimistic
  // message, so their pills only appeared after a page reload. They must ride the
  // optimistic echo from the send moment, then hand off to the server-stored turn.
  it("holds staged attachments on the pending message and sends them", async () => {
    const seen: number[] = [];
    const unsub = useChat.subscribe((s) => seen.push(s.pendingAttachments.length));
    await useChat.getState().send("look at this", null, async () => {}, [ATT]);
    unsub();
    // The optimistic window carried the attachment (pills can render beside the bubble)…
    expect(Math.max(...seen)).toBe(1);
    // …it was cleared once the server-stored turn (which carries its own copy) took over…
    expect(useChat.getState().pendingAttachments).toEqual([]);
    // …and it still rode the request body to the backend.
    const body = sentBodies[0] as { messages: { attachments?: Attachment[] }[] };
    expect(body.messages[0].attachments?.[0]?.att_id).toBe("att-1");
  });

  it("sends no attachments key when nothing is staged", async () => {
    await useChat.getState().send("plain", null, async () => {});
    expect(useChat.getState().pendingAttachments).toEqual([]);
    const body = sentBodies[0] as { messages: { attachments?: Attachment[] }[] };
    expect(body.messages[0].attachments).toBeUndefined();
  });

  it("clears optimistic attachments when the session changes", () => {
    useChat.setState({ pendingAttachments: [ATT] });
    useChat.getState().newSession();
    expect(useChat.getState().pendingAttachments).toEqual([]);
    useChat.setState({ pendingAttachments: [ATT] });
    useChat.getState().openSession("other");
    expect(useChat.getState().pendingAttachments).toEqual([]);
  });
});
