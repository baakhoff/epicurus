import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";
import { useConnection } from "@/stores/connection";

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    models: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    suggestions: vi.fn().mockResolvedValue([]),
    modules: vi.fn().mockResolvedValue([]),
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

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  useConnection.setState({ online: true, coreDown: false });
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

// The composer while disconnected (#494): the draft is kept (it already persists) and
// Send is disabled behind a hint — a message must not fail into an error card for a
// reason the shell banner already explains.
describe("composer while unreachable (#494)", () => {
  it("disables Send with a hint, holds the draft on Enter, and re-arms on recovery", async () => {
    render(<ChatScreen />, { wrapper });
    const composer = await screen.findByLabelText("Message");

    act(() => {
      useChat.setState({ draft: "hello there" });
      useConnection.getState().reportUnreachable();
    });

    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByText(/your draft is kept/i)).toBeInTheDocument();

    // Enter-to-send flows through the same gate as the button — the draft survives
    // untouched (chat.send would have cleared it had the turn started).
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(useChat.getState().draft).toBe("hello there");
    expect(useChat.getState().streaming).toBe(false);

    act(() => useConnection.getState().reportReachable());
    expect(screen.getByRole("button", { name: "Send" })).not.toBeDisabled();
    expect(screen.queryByText(/your draft is kept/i)).not.toBeInTheDocument();
  });

  it("treats device-offline the same as core-down", async () => {
    render(<ChatScreen />, { wrapper });
    await screen.findByLabelText("Message");
    act(() => {
      useChat.setState({ draft: "still here" });
      useConnection.getState().setOnline(false);
    });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByText(/your draft is kept/i)).toBeInTheDocument();
  });
});
