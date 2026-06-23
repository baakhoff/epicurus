import { beforeEach, describe, expect, it, vi } from "vitest";

// `send` opens an SSE stream; stub it with an empty iterable so the store's send
// logic runs to completion without touching the network.
vi.mock("@/lib/sse", () => ({ sse: () => [] }));

import { useChat } from "@/stores/chat";

beforeEach(() => useChat.setState({ draft: "", streaming: false, abort: null }));

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
