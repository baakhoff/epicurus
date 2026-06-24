import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SseMessage } from "@/lib/sse";

// A scripted stream that interleaves thinking, a tool, more thinking, then the answer —
// to prove the store records them in `segments` in the order they arrived (#300).
vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  const frame = (data: unknown): SseMessage => ({ event: "message", data: JSON.stringify(data) });
  return {
    ...actual,
    sse: async function* (): AsyncGenerator<SseMessage> {
      yield frame({ type: "thinking", text: "plan " });
      yield frame({ type: "thinking", text: "the search" });
      yield frame({ type: "tool", tool: "knowledge_search", status: "running" });
      yield frame({ type: "tool", tool: "knowledge_search", status: "ok" });
      yield frame({ type: "thinking", text: "now answer" });
      yield frame({ type: "delta", text: "here it is" });
      yield frame({
        type: "done",
        turn: { content: "here it is", tools_used: ["knowledge_search"], stopped: "completed" },
      });
    },
  };
});

import { useChat, type ChatSegment } from "@/stores/chat";

beforeEach(() => {
  useChat.getState().newSession();
  useChat.setState({ draft: "", streaming: false, abort: null });
});

describe("chat activity ordering (#300)", () => {
  it("records thinking, tool, and text segments in stream order (coalescing thinking)", async () => {
    // `done` clears segments once the server turn takes over, so capture the richest
    // mid-stream snapshot.
    let richest: ChatSegment[] = [];
    const unsub = useChat.subscribe((s) => {
      if (s.segments.length >= richest.length) richest = s.segments;
    });
    await useChat.getState().send("find it", null, async () => {});
    unsub();

    expect(richest.map((s) => s.kind)).toEqual(["thinking", "tool", "thinking", "text"]);
    // consecutive reasoning coalesced into one block
    const first = richest[0];
    expect(first.kind === "thinking" && first.text).toBe("plan the search");
    // the tool sits between the two thinking blocks, and the answer text comes last
    const tool = richest[1];
    expect(tool.kind === "tool" && tool.run.tool).toBe("knowledge_search");
    const third = richest[2];
    expect(third.kind === "thinking" && third.text).toBe("now answer");
  });
});
