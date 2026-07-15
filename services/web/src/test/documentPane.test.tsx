/**
 * The document pane (#541, ADR-0101): what the agent is writing, live beside the chat.
 *
 * The pane's whole correctness question is *what actually happened to the write*. Knowledge and
 * notes **propose** documents (ADR-0033) — with review on (the default) nothing is written, so
 * offering an editor would be a lie. These tests pin both branches, and that the pane never
 * gets in the turn's way.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PanelHost } from "@/components/Panel";
import { api } from "@/lib/api";
import { useChat, type LiveDocument } from "@/stores/chat";
import { usePanel } from "@/stores/panel";

vi.mock("@/lib/api", () => ({
  api: { modules: vi.fn(), suggestionsEnabled: vi.fn() },
}));

// The editor archetype is a page-sized component with its own queries; the pane's job is to
// decide *whether* to hand over to it, which is what these tests are about.
vi.mock("@/components/archetypes/EditorView", () => ({
  EditorView: ({ module, pageId, doc }: { module: string; pageId: string; doc?: string }) => (
    <div data-testid="editor">{`editor:${module}/${pageId}:${doc}`}</div>
  ),
}));

const KNOWLEDGE = {
  manifest: {
    name: "knowledge",
    version: "1.0.0",
    pages: [
      { id: "vault", title: "Knowledge", archetype: "editor", icon: "book", nav_order: 10 },
      { id: "review", title: "Suggestions", archetype: "review", icon: "check", nav_order: 20 },
    ],
    tools: [],
  },
  status: { healthy: true },
  enabled: true,
};

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const doc = (over: Partial<LiveDocument> = {}): LiveDocument => ({
  module: "knowledge",
  content: "# Goals\n\nship the pane",
  target: "projects/goals.md",
  title: null,
  tool: "knowledge_create_document",
  writing: false,
  failed: false,
  dismissed: false,
  ...over,
});

/** Put the pane on screen the way the chat's effect does. */
function openPane(document: LiveDocument) {
  usePanel.getState().close();
  usePanel.getState().open("document", document, "Document");
}

/** `PanelHost` mounts the desktop *and* mobile hosts and lets CSS pick, so every query has to
 *  name one or it matches twice. These assertions are about the view, not the host. */
function panel() {
  return within(screen.getByLabelText("Detail panel"));
}

beforeEach(() => {
  vi.mocked(api.modules).mockResolvedValue([KNOWLEDGE] as never);
  vi.mocked(api.suggestionsEnabled).mockResolvedValue({ enabled: true });
  usePanel.setState({ stack: [] });
  useChat.setState({ liveDocument: null });
});

describe("The document pane while the agent writes (#541)", () => {
  it("shows the document body as it is being written", async () => {
    openPane(doc({ writing: true }));
    render(<PanelHost />, { wrapper });

    expect(await panel().findByText("ship the pane")).toBeInTheDocument();
    expect(panel().getByText("writing…")).toBeInTheDocument();
    // Read-only in flight: a user edit must not race the agent's own write.
    expect(panel().queryByTestId("editor")).not.toBeInTheDocument();
  });

  it("names the target so you can see what is being written before it lands", async () => {
    openPane(doc({ writing: true }));
    render(<PanelHost />, { wrapper });
    expect(await panel().findByText("projects/goals.md")).toBeInTheDocument();
  });
});

describe("The document pane once the write settles (#541, ADR-0033)", () => {
  it("offers review — not an editor — when the write was only staged", async () => {
    // Review on (the default): knowledge_create_document staged a suggestion and wrote nothing.
    vi.mocked(api.suggestionsEnabled).mockResolvedValue({ enabled: true });
    openPane(doc());
    render(<PanelHost />, { wrapper });

    expect(await panel().findByRole("button", { name: "Review & approve" })).toBeInTheDocument();
    expect(panel().getByText(/nothing is written until you approve/i)).toBeInTheDocument();
    // The document does not exist yet — an editor over it would be fiction.
    expect(panel().queryByTestId("editor")).not.toBeInTheDocument();
  });

  it("becomes the real editor when the write actually landed", async () => {
    // Review off: the module applied the change directly, so there is a document to edit.
    vi.mocked(api.suggestionsEnabled).mockResolvedValue({ enabled: false });
    openPane(doc());
    render(<PanelHost />, { wrapper });

    // The editor archetype itself (ADR-0022/0026) — auto-save and version history come with it,
    // through the same module document APIs. No second write path.
    expect(await panel().findByTestId("editor")).toHaveTextContent(
      "editor:knowledge/vault:projects/goals.md",
    );
    expect(panel().queryByRole("button", { name: "Review & approve" })).not.toBeInTheDocument();
  });

  it("says nothing was saved when the write failed", async () => {
    openPane(doc({ failed: true }));
    render(<PanelHost />, { wrapper });

    expect(await panel().findByText(/nothing was saved/i)).toBeInTheDocument();
    expect(panel().queryByTestId("editor")).not.toBeInTheDocument();
  });

  it("stays a preview when the module has no editor page to hand over to", async () => {
    vi.mocked(api.suggestionsEnabled).mockResolvedValue({ enabled: false });
    vi.mocked(api.modules).mockResolvedValue([
      { ...KNOWLEDGE, manifest: { ...KNOWLEDGE.manifest, pages: [] } },
    ] as never);
    openPane(doc());
    render(<PanelHost />, { wrapper });

    expect(await panel().findByText("ship the pane")).toBeInTheDocument();
    expect(panel().queryByTestId("editor")).not.toBeInTheDocument();
  });
});

describe("Dismissing the document pane (#541)", () => {
  it("closing the pane dismisses it, so the chat's re-open effect leaves it shut", async () => {
    useChat.setState({ liveDocument: doc() });
    openPane(doc());
    render(<PanelHost />, { wrapper });

    // The pane is an artifact to watch, not a decision that must be resolved — so it closes.
    (await panel().findByRole("button", { name: "Close panel" })).click();

    await waitFor(() => expect(usePanel.getState().stack).toHaveLength(0));
    // Dismissal is what makes it stick: the chat re-opens the pane while a write is live.
    expect(useChat.getState().liveDocument?.dismissed).toBe(true);
  });
});
