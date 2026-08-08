import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Invisible chats (#772): the ghost toggle left of the model chooser starts a fresh
// invisible session; the header states the deal plainly while one is active; every exit
// path deletes it; the sessions fetch names the live one so the server sweep spares it.
const mockMarkEphemeral = vi.fn();
const mockDeleteSession = vi.fn();
const mockSessions = vi.fn();

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    detail = "";
  },
  api: {
    models: vi
      .fn()
      .mockResolvedValue([
        { name: "qwen3:14b", size: 1, loaded: true, hidden: false, capabilities: [] },
      ]),
    providers: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    suggestions: vi.fn().mockResolvedValue([]),
    activeRun: vi.fn().mockResolvedValue(null),
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    modelDetails: vi.fn().mockResolvedValue({ capabilities: [] }),
    deleteSession: (id: string) => mockDeleteSession(id),
    activeRuns: vi.fn().mockResolvedValue({ session_ids: [] }),
    sessions: (active?: string) => mockSessions(active),
    markEphemeral: (id: string) => mockMarkEphemeral(id),
    modules: vi.fn().mockResolvedValue([]),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    }),
  },
}));

import { ChatScreen } from "@/screens/ChatScreen";
import { fetchSessions, useChat } from "@/stores/chat";
import { usePrefs } from "@/stores/prefs";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockMarkEphemeral.mockReset().mockResolvedValue({ ephemeral: true });
  mockDeleteSession.mockReset().mockResolvedValue({ deleted: 0 });
  mockSessions.mockReset().mockResolvedValue([]);
  usePrefs.setState({ model: null });
  useChat.setState({
    sessionId: "current",
    invisible: false,
    draft: "",
    streaming: false,
    abort: null,
    segments: [],
    pendingUser: null,
    pendingAttachments: [],
    awaiting: null,
    error: null,
  });
  localStorage.clear();
  sessionStorage.clear();
});

describe("invisible-chat store lifecycle (#772)", () => {
  it("toggling on starts a FRESH invisible session and flags it server-side", () => {
    useChat.getState().toggleInvisible();
    const state = useChat.getState();
    expect(state.invisible).toBe(true);
    expect(state.sessionId).not.toBe("current"); // never converts the open chat
    expect(mockMarkEphemeral).toHaveBeenCalledWith(state.sessionId);
    expect(mockDeleteSession).not.toHaveBeenCalled(); // the normal chat is left alone
  });

  it("toggling off is an exit: the invisible chat is deleted, a fresh normal one starts", () => {
    useChat.getState().toggleInvisible();
    const ghost = useChat.getState().sessionId;
    useChat.getState().toggleInvisible();
    const state = useChat.getState();
    expect(state.invisible).toBe(false);
    expect(state.sessionId).not.toBe(ghost);
    expect(mockDeleteSession).toHaveBeenCalledWith(ghost);
  });

  it("switching sessions and starting a new chat are exits too", () => {
    useChat.getState().toggleInvisible();
    const first = useChat.getState().sessionId;
    useChat.getState().openSession("some-old-chat");
    expect(mockDeleteSession).toHaveBeenCalledWith(first);
    expect(useChat.getState().invisible).toBe(false);

    useChat.getState().toggleInvisible();
    const second = useChat.getState().sessionId;
    useChat.getState().newSession();
    expect(mockDeleteSession).toHaveBeenCalledWith(second);
    expect(useChat.getState().invisible).toBe(false);
  });

  it("a normal chat's newSession/openSession never issue a delete", () => {
    useChat.getState().newSession();
    useChat.getState().openSession("other");
    expect(mockDeleteSession).not.toHaveBeenCalled();
  });

  it("fetchSessions names the live invisible session so the sweep spares it", async () => {
    await fetchSessions();
    expect(mockSessions).toHaveBeenLastCalledWith(undefined); // normal chat: no param
    useChat.getState().toggleInvisible();
    await fetchSessions();
    expect(mockSessions).toHaveBeenLastCalledWith(useChat.getState().sessionId);
  });
});

describe("invisible-chat header state (#772)", () => {
  it("shows the ghost toggle and, while active, an unmistakable header + footnote", async () => {
    render(<ChatScreen />, { wrapper });
    const toggle = await screen.findByRole("button", { name: "New invisible chat" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);
    expect(
      await screen.findByRole("heading", { name: /Invisible — deleted when you leave/ }),
    ).toBeInTheDocument();
    const active = screen.getByRole("button", { name: "Leave invisible chat (deletes it)" });
    expect(active).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/deleted when you leave and nothing is learned/)).toBeInTheDocument();
    // The normal-chat memory footnote is replaced, not merely joined.
    expect(screen.queryByText(/remembered across chats/)).toBeNull();

    fireEvent.click(active);
    expect(await screen.findByRole("heading", { name: "New conversation" })).toBeInTheDocument();
    expect(screen.getByText(/remembered across chats/)).toBeInTheDocument();
  });
});

describe("invisible-chat launch guard (#772)", () => {
  it("a same-tab reload resumes the invisible chat, re-marking it", async () => {
    useChat.getState().toggleInvisible();
    const ghost = useChat.getState().sessionId;
    mockMarkEphemeral.mockClear();
    // The sessionStorage marker survived the reload → mounting resumes, re-marks.
    render(<ChatScreen />, { wrapper });
    await waitFor(() => expect(mockMarkEphemeral).toHaveBeenCalledWith(ghost));
    expect(useChat.getState().sessionId).toBe(ghost);
    expect(useChat.getState().invisible).toBe(true);
  });

  it("a fresh launch treats a persisted invisible chat as closed: deleted, fresh start", async () => {
    useChat.setState({ invisible: true, sessionId: "stranded-ghost" });
    sessionStorage.clear(); // a new tab / relaunch: the per-tab marker is gone
    render(<ChatScreen />, { wrapper });
    await waitFor(() => expect(mockDeleteSession).toHaveBeenCalledWith("stranded-ghost"));
    expect(useChat.getState().invisible).toBe(false);
    expect(useChat.getState().sessionId).not.toBe("stranded-ghost");
  });
});
