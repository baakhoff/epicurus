import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";

// The composer's height is grown imperatively on each keystroke; the screen never lays
// out in jsdom, so these tests drive the height by hand and assert the send handler
// clears it. The API + SSE are stubbed so a send completes without touching the network.

vi.mock("@/lib/api", () => ({
  api: {
    models: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    // No in-flight run to recover (#376) — the mount re-attach effect is a clean no-op.
    activeRun: vi.fn().mockResolvedValue(null),
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      hidden: [],
    }),
  },
}));

vi.mock("@/lib/sse", () => ({
  // Yield a real `done` frame so chat.send() reaches a terminal and resolves cleanly (a stream
  // that ends with *no* terminal frame is now treated as a dropped connection, #376).
  async *sse() {
    yield { event: "done", data: JSON.stringify({ type: "done" }) };
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  useChat.setState({
    draft: "",
    streaming: false,
    segments: [],
    pendingUser: null,
    readiness: null,
    error: null,
    paused: false,
    abort: null,
  });
});

describe("Chat composer", () => {
  it("clears its grown height when a message is sent", async () => {
    render(<ChatScreen />, { wrapper });

    const textarea = (await screen.findByLabelText("Message")) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "line one\nline two\nline three" } });
    // Simulate the multi-line growth the keystroke handler would apply with real layout.
    textarea.style.height = "120px";

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    // Inline height cleared → the min-h-[42px] class governs again (one line).
    expect(textarea.style.height).toBe("");
    await waitFor(() => expect(useChat.getState().streaming).toBe(false));
  });

  it("does not send (or reset) an empty draft", async () => {
    render(<ChatScreen />, { wrapper });

    const textarea = (await screen.findByLabelText("Message")) as HTMLTextAreaElement;
    textarea.style.height = "120px";
    // With an empty draft the Send button is disabled, so a click is a no-op.
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(textarea.style.height).toBe("120px");
    expect(useChat.getState().streaming).toBe(false);
  });
});
