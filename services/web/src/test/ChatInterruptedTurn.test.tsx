/**
 * A reply that was cut short says so on the message itself (#944 part 3, ADR-0142).
 *
 * The owner's report was a transcript ending in a lead-in sentence with nothing after it: no
 * banner, no note, just Copy / Regenerate. The live `error` frame could not help — the stream
 * had already died, and what the shell finally rendered came from history. So the mark lives on
 * the persisted message (`stopped`), which is what these tests drive.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";
import { useConnection } from "@/stores/connection";

vi.mock("@/lib/api", () => ({
  api: {
    models: vi.fn().mockResolvedValue([{ name: "llama3.2", loaded: true, hidden: false }]),
    providers: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn(),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
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

const msg = (id: number, role: string, content: string, stopped?: string) => ({
  id,
  role,
  content,
  created_at: new Date(),
  entity_refs: [],
  attachments: [],
  stopped: stopped ?? null,
});

const INTERRUPTED = /interrupted before it finished/i;

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
  useConnection.setState({ online: true, coreDown: false });
});

describe("an interrupted reply is marked in the transcript (#944)", () => {
  it("shows the notice and a Regenerate action on a reloaded failed turn", async () => {
    vi.mocked(api.sessionMessages).mockResolvedValue([
      msg(1, "user", "yeah, move them over there"),
      msg(2, "assistant", "Let me verify the current state first", "error"),
    ] as never);

    render(<ChatScreen />, { wrapper });

    expect(await screen.findByText("Let me verify the current state first")).toBeInTheDocument();
    expect(screen.getByText(INTERRUPTED)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate response" })).toBeInTheDocument();
  });

  it("leaves an ordinary answer unmarked", async () => {
    vi.mocked(api.sessionMessages).mockResolvedValue([
      msg(1, "user", "a question"),
      msg(2, "assistant", "a whole answer"),
    ] as never);

    render(<ChatScreen />, { wrapper });

    expect(await screen.findByText("a whole answer")).toBeInTheDocument();
    expect(screen.queryByText(INTERRUPTED)).not.toBeInTheDocument();
  });

  it("marks an earlier failed turn too, without offering to regenerate it", async () => {
    // Regenerate only ever re-runs the *last* exchange, so an older interrupted turn gets the
    // explanation without an action that would silently rewrite something else.
    vi.mocked(api.sessionMessages).mockResolvedValue([
      msg(1, "user", "first"),
      msg(2, "assistant", "half of an answer", "error"),
      msg(3, "user", "second"),
      msg(4, "assistant", "a whole answer"),
    ] as never);

    render(<ChatScreen />, { wrapper });

    expect(await screen.findByText("half of an answer")).toBeInTheDocument();
    expect(screen.getAllByText(INTERRUPTED)).toHaveLength(1);
    // The one Regenerate belongs to the last (healthy) answer, not to the interrupted one.
    expect(screen.getAllByRole("button", { name: "Regenerate response" })).toHaveLength(1);
  });

  it("says nothing extra for a turn that stopped for a reason that is not a failure", async () => {
    vi.mocked(api.sessionMessages).mockResolvedValue([
      msg(1, "user", "a long job"),
      msg(2, "assistant", "as far as I got", "max_steps"),
    ] as never);

    render(<ChatScreen />, { wrapper });

    expect(await screen.findByText("as far as I got")).toBeInTheDocument();
    expect(screen.queryByText(INTERRUPTED)).not.toBeInTheDocument();
  });
});
