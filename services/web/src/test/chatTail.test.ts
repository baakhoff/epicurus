import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SseMessage } from "@/lib/sse";

// Record the path + body each turn streams to, so the regenerate/edit actions can be
// checked against the right session endpoint (#302).
const calls: { path: string; body: Record<string, unknown> }[] = [];

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return {
    ...actual,
    sse: async function* (path: string, body: Record<string, unknown>): AsyncGenerator<SseMessage> {
      calls.push({ path, body });
      yield {
        event: "done",
        data: JSON.stringify({
          type: "done",
          turn: { content: "fresh", tools_used: [], stopped: "completed" },
        }),
      };
    },
  };
});

import { useChat } from "@/stores/chat";

beforeEach(() => {
  calls.length = 0;
  useChat.getState().newSession();
  useChat.setState({ draft: "", streaming: false, abort: null, pendingUser: "stale" });
});

describe("regenerate / edit the conversation tail (#302)", () => {
  it("regenerate streams to the session's regenerate endpoint, no user echo", async () => {
    const sid = useChat.getState().sessionId;
    await useChat.getState().regenerate("qwen2.5:7b", async () => {});
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe(`/platform/v1/agent/sessions/${encodeURIComponent(sid)}/regenerate`);
    expect(calls[0].body.model).toBe("qwen2.5:7b");
    // No optimistic user echo (the user message is unchanged), and the live turn cleared.
    expect(useChat.getState().pendingUser).toBeNull();
    expect(useChat.getState().streaming).toBe(false);
  });

  it("editAndRerun posts the corrected content to the edit endpoint", async () => {
    const sid = useChat.getState().sessionId;
    await useChat.getState().editAndRerun("corrected ask", null, async () => {});
    expect(calls[0].path).toBe(`/platform/v1/agent/sessions/${encodeURIComponent(sid)}/edit`);
    expect(calls[0].body.content).toBe("corrected ask");
    expect(calls[0].body.model).toBeUndefined();
    expect(useChat.getState().pendingUser).toBeNull();
  });

  it("editAndRerun names the message to revise when given one (#552)", async () => {
    await useChat.getState().editAndRerun("reworded", null, async () => {}, 42);
    expect(calls[0].body.content).toBe("reworded");
    expect(calls[0].body.message_id).toBe(42);
  });

  it("editAndRerun sends no message_id for the last user message (#302's callers, #552)", async () => {
    await useChat.getState().editAndRerun("corrected ask", null, async () => {});
    expect(calls[0].body.message_id).toBeUndefined();
    // Absent on the wire, not null: JSON.stringify drops undefined, and the server reads
    // absence as "the last user message" — so a pre-#552 caller keeps working untouched.
    expect(JSON.parse(JSON.stringify(calls[0].body))).not.toHaveProperty("message_id");
  });

  it("refetches via onDone when the turn completes", async () => {
    const onDone = vi.fn().mockResolvedValue(undefined);
    await useChat.getState().regenerate(null, onDone);
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});
