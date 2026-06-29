import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Attachment } from "@/lib/contracts";
import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";

// The screen reads the session transcript + model lists through the API; stub them so the
// transcript is empty and the only user message on screen is the optimistic (pending) one.
vi.mock("@/lib/api", () => ({
  api: {
    models: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
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

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const ATT: Attachment = { att_id: "att-1", source: "chat", kind: "file", title: "report.pdf" };

beforeEach(() => {
  useChat.setState({
    draft: "",
    streaming: false,
    segments: [],
    pendingUser: null,
    pendingAttachments: [],
    readiness: null,
    error: null,
    paused: false,
    abort: null,
  });
});

describe("Chat optimistic attachment pill", () => {
  // The bug this guards: an attachment shown only after a page reload. The pill must render
  // beside the just-sent (optimistic) message — before any server-history refetch.
  it("renders a pill for a just-sent attachment, before any reload", async () => {
    useChat.setState({ pendingUser: "look at this", pendingAttachments: [ATT] });
    render(<ChatScreen />, { wrapper });
    await waitFor(() => expect(screen.getByText("look at this")).toBeInTheDocument());
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
  });

  it("renders no pill when the pending message has no attachments", async () => {
    useChat.setState({ pendingUser: "plain message", pendingAttachments: [] });
    render(<ChatScreen />, { wrapper });
    await waitFor(() => expect(screen.getByText("plain message")).toBeInTheDocument());
    expect(screen.queryByText("report.pdf")).toBeNull();
  });
});
