import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { SseMessage } from "@/lib/sse";

// Capture every POST turn the store opens (the resolve call records its path + body) and let
// each test script the frames the turn emits — same harness as AskUserPrompt.test.tsx.
const sseCalls: { path: string; body: unknown }[] = [];
let sseScript: () => AsyncGenerator<SseMessage>;

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return {
    ...actual,
    sse: (path: string, body: unknown) => {
      sseCalls.push({ path, body });
      return sseScript();
    },
  };
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
    approveSuggestion: vi.fn().mockResolvedValue({}),
    rejectSuggestion: vi.fn().mockResolvedValue({}),
    resolveEntity: vi.fn().mockResolvedValue(null),
  },
}));

import { api } from "@/lib/api";
import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";
import { useConnection } from "@/stores/connection";
import { usePrefs } from "@/stores/prefs";

const REF = { ref_id: "sugg-1", module: "knowledge", kind: "suggestion", title: "Update goals.md" };

const done = (): SseMessage => ({
  event: "done",
  data: JSON.stringify({ type: "done", turn: { content: "ok", tools_used: [], stopped: "completed" } }),
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  sseCalls.length = 0;
  sseScript = async function* () {
    yield done();
  };
  (api.approveSuggestion as Mock).mockReset().mockResolvedValue({});
  (api.rejectSuggestion as Mock).mockReset().mockResolvedValue({});
  usePrefs.setState({ model: null });
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
    awaitingApproval: null,
  });
  useConnection.setState({ online: true, coreDown: false });
  localStorage.clear();
});

describe("ApprovalPrompt — the ask_approval card (#745, ADR-0117)", () => {
  it("renders the summary, a ref chip, and Approve/Reject", async () => {
    useChat.setState({
      awaitingApproval: { runId: "run-7", summary: "Update goals.md to add a Q3 milestone", refs: [REF] },
    });
    render(<ChatScreen />, { wrapper });

    expect(await screen.findByText("Update goals.md to add a Q3 milestone")).toBeInTheDocument();
    // The chip's own button, not the (always-present but CSS-hidden) hover-card beneath it.
    expect(screen.getByRole("button", { name: "Update goals.md" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
  });

  it("shows a generic prompt when the summary is blank", async () => {
    useChat.setState({ awaitingApproval: { runId: "run-3", summary: "", refs: [] } });
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByText(/staged a change and is waiting/i)).toBeInTheDocument();
  });

  it("renders no ref chips when refs is empty", async () => {
    useChat.setState({ awaitingApproval: { runId: "run-3", summary: "Did a thing", refs: [] } });
    render(<ChatScreen />, { wrapper });
    await screen.findByText("Did a thing");
    expect(screen.queryByRole("button", { name: /update goals/i })).not.toBeInTheDocument();
  });

  it("Approve calls the module's approve endpoint, resolves the run, and dismisses the card", async () => {
    useChat.setState({
      awaitingApproval: { runId: "run-7", summary: "Approve this change?", refs: [REF] },
    });
    render(<ChatScreen />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /approve/i }));

    await waitFor(() => expect(useChat.getState().awaitingApproval).toBeNull());
    expect(api.approveSuggestion).toHaveBeenCalledWith("knowledge", "review", "sugg-1");
    const call = sseCalls.find((c) => c.path.includes("/approval"));
    expect(call?.path).toBe("/platform/v1/agent/runs/run-7/approval");
    expect(call?.body).toEqual({ decision: "approved" });
  });

  it("Reject calls the module's reject endpoint and resolves the run", async () => {
    useChat.setState({
      awaitingApproval: { runId: "run-7", summary: "Approve this change?", refs: [REF] },
    });
    render(<ChatScreen />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /reject/i }));

    await waitFor(() => expect(useChat.getState().awaitingApproval).toBeNull());
    expect(api.rejectSuggestion).toHaveBeenCalledWith("knowledge", "review", "sugg-1");
    const call = sseCalls.find((c) => c.path.includes("/approval"));
    expect(call?.body).toEqual({ decision: "rejected" });
  });

  // Approve/Reject are send-adjacent (#494/#530) the same way ask_user's resume is — both make
  // a real module call, so both must be gated, unlike a draft's safe-to-click-offline Decline.
  it("disables Approve and Reject while the core is unreachable", async () => {
    useChat.setState({
      awaitingApproval: { runId: "run-7", summary: "Approve this change?", refs: [REF] },
    });
    render(<ChatScreen />, { wrapper });
    await screen.findByRole("button", { name: /approve/i });

    act(() =>
      useConnection
        .getState()
        .reportUnreachable({ method: "GET", path: "/platform/v1/power", kind: "502" }),
    );

    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
    expect(sseCalls.some((c) => c.path.includes("/approval"))).toBe(false);
  });
});
