import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The conversations list (SessionsSheet) marks sessions with an in-flight turn (#396). Mock the
// API so two sessions list and `activeRuns` reports which are generating.
const mockActiveRuns = vi.fn();

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    detail = "";
  },
  api: {
    models: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    suggestions: vi.fn().mockResolvedValue([]),
    activeRun: vi.fn().mockResolvedValue(null),
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    modelDetails: vi.fn().mockResolvedValue({ capabilities: [] }),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    }),
    sessions: vi.fn().mockResolvedValue([
      { id: "s1", title: "Alpha", message_count: 3, last_at: new Date("2026-06-29T10:00:00Z") },
      { id: "s2", title: "Beta", message_count: 1, last_at: new Date("2026-06-29T09:00:00Z") },
    ]),
    activeRuns: () => mockActiveRuns(),
  },
}));

import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";
import { usePrefs } from "@/stores/prefs";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

// Scoped to the sheet: the chat header now names the open conversation too (#480), so an
// unscoped title lookup would match twice when the current session is in the list.
const row = (text: string): HTMLElement =>
  within(screen.getByRole("dialog")).getByText(text).closest(".group") as HTMLElement;

beforeEach(() => {
  mockActiveRuns.mockReset().mockResolvedValue({ session_ids: ["s1"] });
  usePrefs.setState({ model: null });
  useChat.setState({
    sessionId: "current",
    streaming: false,
    abort: null,
    segments: [],
    pendingUser: null,
    pendingAttachments: [],
  });
  localStorage.clear();
});

describe("Conversations list running indicator (#396)", () => {
  it("marks only the sessions with an in-flight turn as generating", async () => {
    render(<ChatScreen />, { wrapper });
    fireEvent.click(screen.getByLabelText("Conversations"));

    await screen.findByText("Alpha");
    await waitFor(() => expect(mockActiveRuns).toHaveBeenCalled());
    // s1 (Alpha) is generating; s2 (Beta) is idle.
    expect(within(row("Alpha")).getByLabelText("Generating")).toBeInTheDocument();
    expect(within(row("Beta")).queryByLabelText("Generating")).toBeNull();
  });

  it("shows no indicator when nothing is running", async () => {
    mockActiveRuns.mockResolvedValue({ session_ids: [] });
    render(<ChatScreen />, { wrapper });
    fireEvent.click(screen.getByLabelText("Conversations"));

    await screen.findByText("Alpha");
    await waitFor(() => expect(mockActiveRuns).toHaveBeenCalled());
    expect(screen.queryByLabelText("Generating")).toBeNull();
  });

  it("marks the current session from its live streaming state, before the poll catches up", async () => {
    // The server hasn't registered the run yet, but the current session is streaming locally.
    mockActiveRuns.mockResolvedValue({ session_ids: [] });
    // `abort` set so the mount-time re-attach is a no-op and `streaming` stays true.
    useChat.setState({ sessionId: "s2", streaming: true, abort: new AbortController() });
    render(<ChatScreen />, { wrapper });
    fireEvent.click(screen.getByLabelText("Conversations"));

    await within(await screen.findByRole("dialog")).findByText("Beta");
    expect(within(row("Beta")).getByLabelText("Generating")).toBeInTheDocument();
  });
});
