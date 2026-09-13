import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SseMessage } from "@/lib/sse";

// A turn that works through several tool rounds before answering. The operator's round bound has
// no ceiling since #925, so a turn like this can legitimately run for minutes — the store has to
// carry where it is so the activity indicator can say so instead of spinning silently.
vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  const frame = (data: unknown): SseMessage => ({ event: "message", data: JSON.stringify(data) });
  return {
    ...actual,
    sse: async function* (): AsyncGenerator<SseMessage> {
      yield frame({ type: "tool", tool: "websearch", status: "running", round: 1, max_rounds: 40 });
      yield frame({ type: "tool", tool: "websearch", status: "ok", round: 1, max_rounds: 40 });
      yield frame({
        type: "tool",
        tool: "knowledge_search",
        status: "running",
        round: 7,
        max_rounds: 40,
      });
      yield frame({
        type: "tool",
        tool: "knowledge_search",
        status: "ok",
        round: 7,
        max_rounds: 40,
      });
      yield frame({ type: "delta", text: "here it is" });
      yield frame({
        type: "done",
        turn: {
          content: "here it is",
          tools_used: ["websearch", "knowledge_search"],
          stopped: "completed",
          rounds: 8,
          max_rounds: 40,
        },
      });
    },
  };
});

import { useChat } from "@/stores/chat";

beforeEach(() => {
  useChat.getState().newSession();
  useChat.setState({ draft: "", streaming: false, abort: null });
});

describe("round progress on a live turn (#925)", () => {
  it("tracks the round the turn is on, and clears it when the turn ends", async () => {
    const seen: Array<{ round: number; maxRounds: number } | null> = [];
    const unsub = useChat.subscribe((s) => {
      const last = seen[seen.length - 1];
      if (last?.round !== s.progress?.round) seen.push(s.progress);
    });
    await useChat.getState().send("find it", null, async () => {});
    unsub();

    // It advances with the stream rather than counting frames: round 7 arrives as 7, not as 2.
    expect(seen.filter(Boolean)).toEqual([
      { round: 1, maxRounds: 40 },
      { round: 7, maxRounds: 40 },
    ]);
    // Once the turn is over the live copy is gone — the persisted message takes over, and a
    // stale "round 7 of 40" hanging under a finished answer would be its own small lie.
    expect(useChat.getState().progress).toBeNull();
  });
});

describe("a turn with no round information (#925)", () => {
  it("leaves progress null so the indicator looks exactly as it did before", async () => {
    // An older core — or any tool frame without the additive fields — must change nothing.
    vi.resetModules();
    vi.doMock("@/lib/sse", async (importOriginal) => {
      const actual = await importOriginal<typeof import("@/lib/sse")>();
      const frame = (data: unknown): SseMessage => ({
        event: "message",
        data: JSON.stringify(data),
      });
      return {
        ...actual,
        sse: async function* (): AsyncGenerator<SseMessage> {
          yield frame({ type: "tool", tool: "websearch", status: "running" });
          yield frame({ type: "tool", tool: "websearch", status: "ok" });
          yield frame({ type: "delta", text: "done" });
          yield frame({
            type: "done",
            turn: { content: "done", tools_used: ["websearch"], stopped: "completed" },
          });
        },
      };
    });
    const { useChat: freshChat } = await import("@/stores/chat");
    freshChat.getState().newSession();
    freshChat.setState({ draft: "", streaming: false, abort: null });

    let everSet = false;
    const unsub = freshChat.subscribe((s) => {
      if (s.progress !== null) everSet = true;
    });
    await freshChat.getState().send("find it", null, async () => {});
    unsub();
    expect(everSet).toBe(false);
  });
});
