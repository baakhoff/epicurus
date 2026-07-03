import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The conversations sheet groups by recency, filters by title, and never deletes without
// confirming (#480). Sessions are pinned to "now"-relative moments so grouping is stable.
// (Both fns are referenced from the hoisted vi.mock factory, so they must be vi.fn()s
// whose behaviour is filled in beforeEach — plain consts are not yet initialized there.)
const mockDeleteSession = vi.fn();
const mockSessionsList = vi.fn();

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    detail = "";
  },
  api: {
    models: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    modules: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    suggestions: vi.fn().mockResolvedValue([]),
    activeRun: vi.fn().mockResolvedValue(null),
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    modelDetails: vi.fn().mockResolvedValue({ capabilities: [] }),
    deleteSession: (id: string) => mockDeleteSession(id),
    activeRuns: vi.fn().mockResolvedValue({ session_ids: [] }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    }),
    sessions: () => mockSessionsList(),
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

async function openSheet() {
  render(<ChatScreen />, { wrapper });
  fireEvent.click(screen.getByLabelText("Conversations"));
  await screen.findByText("Fresh plans");
}

const hour = 3_600_000;
const day = 86_400_000;
const at = (msAgo: number) => new Date(Date.now() - msAgo);

beforeEach(() => {
  mockDeleteSession.mockReset().mockResolvedValue({ deleted: 1 });
  mockSessionsList.mockReset().mockResolvedValue([
    { id: "s-today", title: "Fresh plans", message_count: 3, last_at: at(2 * hour) },
    { id: "s-yesterday", title: "Balcony lamp", message_count: 5, last_at: at(day + 2 * hour) },
    { id: "s-old", title: "Ancient history", message_count: 9, last_at: at(45 * day) },
  ]);
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

describe("Conversations sheet (#480)", () => {
  it("groups sessions under recency headers", async () => {
    await openSheet();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("Yesterday")).toBeInTheDocument();
    expect(screen.getByText("Earlier")).toBeInTheDocument();
    // No empty buckets are rendered.
    expect(screen.queryByText("This week")).toBeNull();
  });

  it("filters by title and flattens the groups while searching", async () => {
    await openSheet();
    fireEvent.change(screen.getByLabelText("Search conversations"), {
      target: { value: "lamp" },
    });
    expect(screen.getByText("Balcony lamp")).toBeInTheDocument();
    expect(screen.queryByText("Fresh plans")).toBeNull();
    expect(screen.queryByText("Yesterday")).toBeNull(); // headers gone in search mode
  });

  it("says so when nothing matches", async () => {
    await openSheet();
    fireEvent.change(screen.getByLabelText("Search conversations"), {
      target: { value: "zebra" },
    });
    expect(screen.getByText(/Nothing matches/)).toBeInTheDocument();
  });

  it("never deletes without confirmation", async () => {
    await openSheet();
    fireEvent.click(screen.getByLabelText("Delete Balcony lamp"));
    expect(mockDeleteSession).not.toHaveBeenCalled(); // the click only opened the dialog
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(mockDeleteSession).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText("Delete Balcony lamp"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mockDeleteSession).toHaveBeenCalledWith("s-yesterday"));
  });

  it("starts a fresh conversation when the open one is deleted", async () => {
    useChat.setState({ sessionId: "s-today" });
    await openSheet();
    fireEvent.click(screen.getByLabelText("Delete Fresh plans"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mockDeleteSession).toHaveBeenCalledWith("s-today"));
    // The orphaned transcript is not left on screen — a new session id takes over.
    await waitFor(() => expect(useChat.getState().sessionId).not.toBe("s-today"));
  });

  it("deleting another conversation leaves the open one alone", async () => {
    useChat.setState({ sessionId: "s-today" });
    await openSheet();
    fireEvent.click(screen.getByLabelText("Delete Ancient history"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mockDeleteSession).toHaveBeenCalledWith("s-old"));
    expect(useChat.getState().sessionId).toBe("s-today");
  });
});
