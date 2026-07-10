import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// "Finished while you were away" (#492): a session leaving the active-run set while it isn't
// the open one reads as an unseen answer. Mirrors SessionsRunning.test.tsx's mock shape.
const mockActiveRuns = vi.fn();

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
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    activeRuns: () => mockActiveRuns(),
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
  },
}));

import { ChatScreen } from "@/screens/ChatScreen";
import { newlyFinished, useAwayFinishedWatch, useChat } from "@/stores/chat";
import { usePrefs } from "@/stores/prefs";

// chat.ts captures `document.title` once at module load (its own BASE_TITLE) — read the same
// value here, right after importing it, rather than assume a literal string (jsdom's default
// differs from the real index.html's <title>).
const BASE_TITLE = document.title;

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockActiveRuns.mockReset().mockResolvedValue({ session_ids: [] });
  usePrefs.setState({ model: null });
  useChat.setState({
    sessionId: "current",
    streaming: false,
    abort: null,
    segments: [],
    pendingUser: null,
    pendingAttachments: [],
    unseenFinished: new Set(),
  });
  document.title = BASE_TITLE;
  localStorage.clear();
});

describe("newlyFinished (#492)", () => {
  it("reports a session that dropped out of the active set", () => {
    expect(newlyFinished(new Set(["a", "b"]), new Set(["a"]), "current")).toEqual(["b"]);
  });

  it("excludes the currently-open session even if it dropped out", () => {
    expect(newlyFinished(new Set(["a", "current"]), new Set(["a"]), "current")).toEqual([]);
  });

  it("reports nothing when the active set is unchanged", () => {
    expect(newlyFinished(new Set(["a", "b"]), new Set(["a", "b"]), "current")).toEqual([]);
  });

  it("ignores a session newly appearing in the active set", () => {
    expect(newlyFinished(new Set(["a"]), new Set(["a", "b"]), "current")).toEqual([]);
  });
});

describe("useAwayFinishedWatch (#492)", () => {
  function Harness() {
    useAwayFinishedWatch();
    return null;
  }

  it("marks a session unseen-finished when a later poll no longer reports it running", async () => {
    mockActiveRuns.mockResolvedValueOnce({ session_ids: ["s1"] });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>,
    );
    // Wait for the first poll's data to actually land in the cache (not just the mock being
    // called) — the watcher's effect must have set its "previous active set" baseline before
    // the next poll, or the transition has nothing to diff against.
    await waitFor(() => expect(qc.getQueryData(["active-runs"])).toEqual({ session_ids: ["s1"] }));
    expect(useChat.getState().unseenFinished.has("s1")).toBe(false); // no prior poll to diff against yet

    mockActiveRuns.mockResolvedValueOnce({ session_ids: [] }); // s1 finished
    await act(() => qc.refetchQueries({ queryKey: ["active-runs"] }));

    await waitFor(() => expect(useChat.getState().unseenFinished.has("s1")).toBe(true));
  });

  it("never marks the currently-open session, even when it drops out of the active set", async () => {
    useChat.setState({ sessionId: "s1" });
    mockActiveRuns.mockResolvedValueOnce({ session_ids: ["s1"] });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(qc.getQueryData(["active-runs"])).toEqual({ session_ids: ["s1"] }));

    mockActiveRuns.mockResolvedValueOnce({ session_ids: [] });
    await act(() => qc.refetchQueries({ queryKey: ["active-runs"] }));
    await waitFor(() => expect(qc.getQueryData(["active-runs"])).toEqual({ session_ids: [] }));

    expect(useChat.getState().unseenFinished.has("s1")).toBe(false);
  });

  it("prefixes the document title while an answer is unseen, restores it once seen", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>,
    );
    expect(document.title).toBe(BASE_TITLE);
    // The DOM's own title getter strips/collapses whitespace (WHATWG "child text content,
    // stripped and collapsed"), so a numerically-correct `"• " + BASE_TITLE` can still read
    // back differently — derive the expectation the same way rather than hand-construct it.
    document.title = `• ${BASE_TITLE}`;
    const expectedPrefixed = document.title;
    document.title = BASE_TITLE;

    act(() => useChat.setState({ unseenFinished: new Set(["s1"]) }));
    await waitFor(() => expect(document.title).toBe(expectedPrefixed));

    act(() => useChat.getState().openSession("s1")); // the normal "seen" path — clears the marker
    await waitFor(() => expect(document.title).toBe(BASE_TITLE));
  });
});

describe("Finished-while-away UI (#492)", () => {
  it("shows an accent dot on the History button while any session is unseen-finished", async () => {
    useChat.setState({ unseenFinished: new Set(["s1"]) });
    render(<ChatScreen />, { wrapper });

    expect(screen.getByLabelText("Conversations (unseen answer)")).toBeInTheDocument();
    expect(screen.queryByLabelText("Conversations")).toBeNull();
  });

  it("shows the plain label and no dot when nothing is unseen", async () => {
    render(<ChatScreen />, { wrapper });

    expect(screen.getByLabelText("Conversations")).toBeInTheDocument();
    expect(screen.queryByLabelText("Conversations (unseen answer)")).toBeNull();
  });

  it("marks the session's row in the sheet, and clears both markers once it's opened", async () => {
    useChat.setState({ unseenFinished: new Set(["s1"]) });
    render(<ChatScreen />, { wrapper });

    fireEvent.click(screen.getByLabelText("Conversations (unseen answer)"));
    const row = (await screen.findByText("Alpha")).closest('[class~="group/session"]') as HTMLElement;
    expect(within(row).getByLabelText("Finished, unseen")).toBeInTheDocument();
    // Beta was never marked.
    const otherRow = screen.getByText("Beta").closest('[class~="group/session"]') as HTMLElement;
    expect(within(otherRow).queryByLabelText("Finished, unseen")).toBeNull();

    fireEvent.click(screen.getByText("Alpha")); // opens it — the sheet's own row-open handler
    await waitFor(() => expect(useChat.getState().unseenFinished.has("s1")).toBe(false));
    expect(screen.getByLabelText("Conversations")).toBeInTheDocument();
  });
});
