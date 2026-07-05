import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SseMessage } from "@/lib/sse";

// Capture every POST turn the store opens (the resume call records its path + body) and let
// each test script the frames the turn emits.
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

// ChatScreen's queries must resolve so the screen renders (same surface as ChatToolless.test).
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
import { usePrefs } from "@/stores/prefs";

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
  });
  useConnection.setState({ online: true, coreDown: false });
  localStorage.clear();
});

describe("AskUserPrompt — the ask_user clarifying prompt (#360)", () => {
  it("renders the pending question with an inline answer input", async () => {
    useChat.setState({ awaiting: { runId: "run-7", question: "Which file did you mean?" } });
    render(<ChatScreen />, { wrapper });

    expect(await screen.findByText("Which file did you mean?")).toBeInTheDocument();
    expect(screen.getByLabelText(/answer the assistant/i)).toBeInTheDocument();
  });

  it("submitting the answer resumes the suspended run and dismisses the prompt", async () => {
    useChat.setState({ awaiting: { runId: "run-7", question: "Which file?" } });
    render(<ChatScreen />, { wrapper });

    const input = await screen.findByLabelText(/answer the assistant/i);
    fireEvent.change(input, { target: { value: "the readme" } });
    fireEvent.click(screen.getByLabelText(/send answer/i));

    await waitFor(() => expect(useChat.getState().awaiting).toBeNull());
    const resumeCall = sseCalls.find((c) => c.path.includes("/resume"));
    expect(resumeCall?.path).toBe("/platform/v1/agent/runs/run-7/resume");
    expect(resumeCall?.body).toEqual({ answer: "the readme" });
  });

  it("keeps Send disabled until the answer has content", async () => {
    useChat.setState({ awaiting: { runId: "run-7", question: "Which file?" } });
    render(<ChatScreen />, { wrapper });

    const button = await screen.findByLabelText(/send answer/i);
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/answer the assistant/i), { target: { value: "x" } });
    expect(button).toBeEnabled();
  });

  it("shows a generic prompt when the question is blank", async () => {
    useChat.setState({ awaiting: { runId: "run-3", question: "" } });
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByText(/needs a little more to go on/i)).toBeInTheDocument();
  });

  // Resuming a suspended turn is send-adjacent the same way the composer's Send is (#494) —
  // gate it on the connection store too, or it fails into the old error card (#530).
  it("disables Send and ignores Enter-to-resume while the core is unreachable", async () => {
    useChat.setState({ awaiting: { runId: "run-7", question: "Which file?" } });
    render(<ChatScreen />, { wrapper });

    const input = await screen.findByLabelText(/answer the assistant/i);
    fireEvent.change(input, { target: { value: "the readme" } });
    act(() => useConnection.getState().reportUnreachable());

    expect(screen.getByLabelText(/send answer/i)).toBeDisabled();

    // Enter-to-submit bypasses the button entirely (composer parity, #494) — the guard
    // inside submit() must catch it too, or the prompt would dismiss on a failed resume.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(useChat.getState().awaiting).not.toBeNull();
    expect(sseCalls.some((c) => c.path.includes("/resume"))).toBe(false);
  });
});
