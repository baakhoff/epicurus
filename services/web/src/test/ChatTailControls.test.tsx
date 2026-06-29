import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";

vi.mock("@/lib/api", () => ({
  api: {
    models: vi.fn().mockResolvedValue([{ name: "llama3.2", loaded: true, hidden: false }]),
    providers: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([
      { role: "user", content: "first question", created_at: new Date(), entity_refs: [], attachments: [] },
      { role: "assistant", content: "an answer", created_at: new Date(), entity_refs: [], attachments: [] },
    ]),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    activeRun: vi.fn().mockResolvedValue(null), // no in-flight run to recover (#376)
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      hidden: [],
    }),
  },
}));

// Streaming isn't exercised here (we assert the controls render, not the re-run).
vi.mock("@/lib/sse", () => ({
  // eslint-disable-next-line require-yield
  async *sse() {
    return;
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

describe("Chat tail controls (#302)", () => {
  it("shows Regenerate on the last answer and Edit on the last user message", async () => {
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByText("an answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate response" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit message" })).toBeInTheDocument();
  });

  it("opens an inline editor seeded with the message when Edit is clicked", async () => {
    render(<ChatScreen />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Edit message" }));
    const editor = (await screen.findByLabelText("Edit message")) as HTMLTextAreaElement;
    expect(editor.value).toBe("first question");
    expect(screen.getByRole("button", { name: "Resend" })).toBeInTheDocument();
    // The Regenerate control hides while editing.
    expect(screen.queryByRole("button", { name: "Regenerate response" })).not.toBeInTheDocument();
  });
});
