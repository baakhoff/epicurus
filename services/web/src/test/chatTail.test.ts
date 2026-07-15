import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SseMessage } from "@/lib/sse";

// Record the path + body each turn streams to, so the regenerate/edit actions can be
// checked against the right session endpoint (#302).
const calls: { path: string; body: Record<string, unknown> }[] = [];
/** The frames the next turn streams; null ⇒ just a `done` (what most tests here want). */
let frames: SseMessage[] | null = null;

const doneFrame = (): SseMessage => ({
  event: "done",
  data: JSON.stringify({
    type: "done",
    turn: { content: "fresh", tools_used: [], stopped: "completed" },
  }),
});

/** A `tool` frame, optionally carrying the document the call writes (#541). */
const toolFrame = (
  tool: string,
  status: "running" | "ok" | "error",
  document?: Record<string, unknown>,
): SseMessage => ({
  event: "tool",
  data: JSON.stringify({ type: "tool", tool, status, document }),
});

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return {
    ...actual,
    sse: async function* (path: string, body: Record<string, unknown>): AsyncGenerator<SseMessage> {
      calls.push({ path, body });
      for (const frame of frames ?? [doneFrame()]) yield frame;
    },
  };
});

import { useChat } from "@/stores/chat";

beforeEach(() => {
  calls.length = 0;
  frames = null;
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

// The document pane's state comes off the tool event (#541, ADR-0101): the core reads the
// module's annotation and says what the call is writing, so the store needn't know any module.
describe("the document a turn is writing (#541)", () => {
  const document = {
    module: "knowledge",
    content: "# Goals",
    target: "projects/goals.md",
    title: null,
  };

  it("has no live document until a tool event carries one", async () => {
    await useChat.getState().send("hi", null, async () => {});
    expect(useChat.getState().liveDocument).toBeNull();
  });

  it("opens on the running frame and settles on the terminal one", async () => {
    frames = [
      toolFrame("knowledge_create_document", "running", document),
      toolFrame("knowledge_create_document", "ok", document),
      doneFrame(),
    ];
    await useChat.getState().send("write it", null, async () => {});

    const live = useChat.getState().liveDocument;
    expect(live).toMatchObject({ ...document, tool: "knowledge_create_document" });
    expect(live?.writing).toBe(false); // the terminal frame unlocks the pane
    expect(live?.failed).toBe(false);
  });

  it("marks a failed write, so the pane can say nothing was saved", async () => {
    frames = [
      toolFrame("knowledge_create_document", "running", document),
      toolFrame("knowledge_create_document", "error", document),
      doneFrame(),
    ];
    await useChat.getState().send("write it", null, async () => {});
    expect(useChat.getState().liveDocument?.failed).toBe(true);
  });

  it("an unannotated tool call leaves the pane alone", async () => {
    frames = [toolFrame("knowledge_search", "ok"), doneFrame()];
    await useChat.getState().send("search", null, async () => {});
    expect(useChat.getState().liveDocument).toBeNull();
  });

  it("a dismissal survives later frames of the same write", async () => {
    frames = [toolFrame("knowledge_create_document", "running", document), doneFrame()];
    await useChat.getState().send("write it", null, async () => {});
    useChat.getState().dismissDocument();

    // The agent finishing the write it already had open is not a reason to overrule the user.
    frames = [toolFrame("knowledge_create_document", "ok", document), doneFrame()];
    await useChat.getState().send("carry on", null, async () => {});
    expect(useChat.getState().liveDocument?.dismissed).toBe(true);
  });

  it("a write to a different document opens afresh", async () => {
    frames = [toolFrame("knowledge_create_document", "ok", document), doneFrame()];
    await useChat.getState().send("write it", null, async () => {});
    useChat.getState().dismissDocument();

    const other = { ...document, target: "projects/other.md" };
    frames = [toolFrame("knowledge_create_document", "running", other), doneFrame()];
    await useChat.getState().send("and another", null, async () => {});
    expect(useChat.getState().liveDocument?.target).toBe("projects/other.md");
    expect(useChat.getState().liveDocument?.dismissed).toBe(false);
  });
});
