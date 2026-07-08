import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SseMessage } from "@/lib/sse";

// A turn never streams in these tests — we set `awaitingDraft` directly to exercise the
// ChatScreen ↔ panel seam. The sse mock just keeps the store's import happy.
vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return { ...actual, sse: async function* (): AsyncGenerator<SseMessage> {} };
});

// ChatScreen's queries must resolve so the screen renders (same surface as AskUserPrompt.test).
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    detail = "";
  },
  api: {
    models: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    suggestions: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    activeRun: vi.fn().mockResolvedValue(null),
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    }),
    modelDetails: vi.fn().mockResolvedValue({ capabilities: [] }),
  },
}));

import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";
import { useConnection } from "@/stores/connection";
import { usePanel } from "@/stores/panel";
import { usePrefs } from "@/stores/prefs";

const DRAFT = { to: "bob@x.com", subject: "Lunch?", body: "Noon works." };

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function topView(): string | null {
  const stack = usePanel.getState().stack;
  return stack[stack.length - 1]?.view ?? null;
}

beforeEach(() => {
  usePrefs.setState({ model: null });
  usePanel.getState().close();
  useConnection.setState({ online: true, coreDown: false });
  useChat.setState({
    sessionId: "s1",
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

describe("draft-review pane ↔ chat store seam (ADR-0085, #563)", () => {
  it("opens the email-draft pane on a draft pause and closes it when resolved", async () => {
    render(<ChatScreen />, { wrapper });
    expect(topView()).toBeNull();

    act(() => useChat.setState({ awaitingDraft: { runId: "run-1", draft: DRAFT } }));
    await waitFor(() => expect(topView()).toBe("email-draft"));

    act(() => useChat.setState({ awaitingDraft: null }));
    await waitFor(() => expect(topView()).toBeNull());
  });

  it("re-opens the pane if it is dismissed while the draft is still pending (no strand)", async () => {
    render(<ChatScreen />, { wrapper });
    act(() => useChat.setState({ awaitingDraft: { runId: "run-1", draft: DRAFT } }));
    await waitFor(() => expect(topView()).toBe("email-draft"));

    // The user clicks the panel's generic Close — the draft is still pending, so it must come back.
    act(() => usePanel.getState().close());
    await waitFor(() => expect(topView()).toBe("email-draft"));

    act(() => useChat.setState({ awaitingDraft: null }));
    await waitFor(() => expect(topView()).toBeNull());
  });

  it("resets rather than pushes — repeated dismiss never grows the stack", async () => {
    render(<ChatScreen />, { wrapper });
    act(() => useChat.setState({ awaitingDraft: { runId: "run-1", draft: DRAFT } }));
    await waitFor(() => expect(topView()).toBe("email-draft"));

    act(() => usePanel.getState().close());
    await waitFor(() => expect(topView()).toBe("email-draft"));
    act(() => usePanel.getState().close());
    await waitFor(() => expect(topView()).toBe("email-draft"));

    expect(usePanel.getState().stack).toHaveLength(1);
  });
});
