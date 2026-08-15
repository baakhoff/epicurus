/**
 * The document pane (#541, ADR-0101): what the agent is writing, live beside the chat, and
 * (#654, ADR-0121) typed into it as the model writes it.
 *
 * The pane's whole correctness question is *what actually happened to the write*. Knowledge and
 * notes **propose** documents (ADR-0033) — with review on (the default) nothing is written, so
 * offering an editor would be a lie. These tests pin both branches, and that the pane never
 * gets in the turn's way. The typewriter adds a second question: what is on screen *before* the
 * write happened is a guess read out of half-typed JSON, so it must never outlive the real thing.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { PanelHost } from "@/components/Panel";
import { api } from "@/lib/api";
import type { SseMessage } from "@/lib/sse";
import { useChat, type LiveDocument } from "@/stores/chat";
import { usePanel } from "@/stores/panel";

vi.mock("@/lib/api", () => ({
  api: { modules: vi.fn(), suggestionsEnabled: vi.fn(), activeRun: vi.fn() },
}));

// The SSE transport, scripted per test: `sse` drives a fresh turn, `sseRequest` a re-attach.
let sseScript: () => AsyncGenerator<SseMessage>;
let reattachScript: () => AsyncGenerator<SseMessage>;

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return { ...actual, sse: () => sseScript(), sseRequest: () => reattachScript() };
});

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

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
  streaming: false,
  ...over,
});

/* ── SSE frames, the shapes the core actually emits ────────────────────────── */

const preview = (
  text: string,
  meta: Record<string, unknown> = { module: "knowledge" },
  id?: string,
): SseMessage => ({
  event: "doc_preview",
  data: JSON.stringify({ type: "doc_preview", tool: "knowledge_create_document", text, preview: meta }),
  id,
});

const toolFrame = (
  status: "running" | "ok" | "error",
  content: string,
  over: Record<string, unknown> = {},
): SseMessage => ({
  event: "tool",
  data: JSON.stringify({
    type: "tool",
    tool: "knowledge_create_document",
    status,
    document: {
      module: "knowledge",
      content,
      target: "projects/goals.md",
      title: null,
      ...over,
    },
  }),
});

const doneFrame = (): SseMessage => ({
  event: "done",
  data: JSON.stringify({
    type: "done",
    turn: { content: "saved", tools_used: ["knowledge_create_document"], stopped: "completed" },
  }),
});

/** Play a scripted stream through the store the way a real turn does. */
async function runTurn(frames: SseMessage[]): Promise<void> {
  sseScript = async function* () {
    for (const frame of frames) yield frame;
  };
  await useChat.getState().send("write it down", null, async () => {});
}

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

async function* nothing(): AsyncGenerator<SseMessage> {
  // a stream that ends with no terminal frame == a dropped connection
}

beforeEach(() => {
  vi.mocked(api.modules).mockResolvedValue([KNOWLEDGE] as never);
  vi.mocked(api.suggestionsEnabled).mockReset().mockResolvedValue({ enabled: true });
  (api.activeRun as Mock).mockReset().mockResolvedValue(null);
  sseScript = nothing;
  reattachScript = nothing;
  usePanel.setState({ stack: [] });
  useChat.setState({ liveDocument: null, segments: [], streaming: false, lastSeq: 0 });
  mockNavigate.mockReset();
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

  it("resolves the review-state query under the shared kebab-case key (#659)", async () => {
    // Was `["suggestionsEnabled", module]` — a duplicate cache entry the review toggle's
    // own `["suggestions-enabled", module]` invalidation never reached. Asserting the exact
    // key (not just that the mock was called) pins the fix specifically.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    openPane(doc());
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <PanelHost />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await panel().findByRole("button", { name: "Review & approve" });
    expect(qc.getQueryData(["suggestions-enabled", "knowledge"])).toEqual({ enabled: true });
    expect(qc.getQueryData(["suggestionsEnabled", "knowledge"])).toBeUndefined();
  });

  it("Review & approve navigates in-app and dismisses the pane, without a hard reload (#659)", async () => {
    // This was the app's only SPA-internal hard `window.location.assign` — it dropped the
    // live SSE stream for no reason. Asserting `navigate()` (not a real page load) and that
    // the pane's own state clears pins both halves of the fix in one place.
    useChat.setState({ liveDocument: doc() });
    openPane(doc());
    render(<PanelHost />, { wrapper });

    fireEvent.click(await panel().findByRole("button", { name: "Review & approve" }));

    expect(mockNavigate).toHaveBeenCalledWith("/m/knowledge/review");
    await waitFor(() => expect(usePanel.getState().stack).toHaveLength(0));
    // Dismissed, not just closed — otherwise the chat's re-open effect would reopen it.
    expect(useChat.getState().liveDocument?.dismissed).toBe(true);
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

describe("The typewriter: the document as the model types it (#654, ADR-0121)", () => {
  it("grows the body one coalesced delta at a time", async () => {
    await runTurn([
      preview("# Goals\n\n"),
      preview("ship the "),
      preview("typewriter"),
      toolFrame("running", "# Goals\n\nship the typewriter"),
      toolFrame("ok", "# Goals\n\nship the typewriter"),
      doneFrame(),
    ]);

    // The deltas concatenate — the client's whole job, and what makes replay on re-attach free.
    expect(useChat.getState().liveDocument?.content).toBe("# Goals\n\nship the typewriter");
  });

  it("opens the pane on the first character, before the call is made", async () => {
    // Snapshots are collected *inside* the stream and asserted after it: an expect() thrown from
    // the generator would be swallowed by the store's mid-stream error handling and read as a
    // dropped connection, quietly passing.
    const seen: (LiveDocument | null)[] = [];
    sseScript = async function* () {
      yield preview("# ");
      seen.push(useChat.getState().liveDocument);
      yield doneFrame();
    };
    await useChat.getState().send("write it", null, async () => {});

    expect(seen[0]).toMatchObject({
      module: "knowledge",
      content: "# ",
      writing: true, // read-only from the very first character
      streaming: true,
      dismissed: false,
    });
  });

  it("fills the header in as the target and title finish typing", async () => {
    await runTurn([
      preview("body "),
      preview("text", { module: "knowledge", target: "projects/goals.md", title: "Goals" }),
      doneFrame(),
    ]);

    const live = useChat.getState().liveDocument;
    expect(live?.target).toBe("projects/goals.md");
    expect(live?.title).toBe("Goals");
    expect(live?.content).toBe("body text"); // metadata arriving doesn't restart the body
  });

  it("hands the pane over to the stored document when the write settles (ADR-0101)", async () => {
    // The preview is a *guess* read out of half-typed JSON — here it drifted (the model's tail
    // never made it into a frame). The `tool` frame carries the call as actually parsed, and it
    // is what the pane must end on: the pane never keeps showing something that wasn't written.
    await runTurn([
      preview("half the "),
      toolFrame("running", "half the document, in full"),
      toolFrame("ok", "half the document, in full"),
      doneFrame(),
    ]);

    const live = useChat.getState().liveDocument;
    expect(live?.content).toBe("half the document, in full");
    expect(live?.streaming).toBe(false); // the typewriter is over…
    expect(live?.writing).toBe(false); // …and so is the write
    expect(live?.failed).toBe(false);
  });

  it("keeps the pane read-only from the first preview through the settle (#541)", async () => {
    // #541's conflict — a user edit racing the agent's write — stays structurally impossible
    // because there is never an editable pane while a write is in flight.
    const writing: (boolean | undefined)[] = [];
    sseScript = async function* () {
      yield preview("typing");
      writing.push(useChat.getState().liveDocument?.writing);
      yield toolFrame("running", "typing done");
      writing.push(useChat.getState().liveDocument?.writing);
      yield toolFrame("ok", "typing done");
      yield doneFrame();
    };
    await useChat.getState().send("write it", null, async () => {});

    expect(writing).toEqual([true, true]);
    expect(useChat.getState().liveDocument?.writing).toBe(false); // only the terminal frame frees it
  });

  it("says nothing was saved when the write the typewriter showed then failed", async () => {
    await runTurn([preview("attempted body"), toolFrame("error", "attempted body"), doneFrame()]);

    const live = useChat.getState().liveDocument;
    expect(live?.failed).toBe(true);
    expect(live?.streaming).toBe(false);
  });

  it("keeps a mid-write dismissal dismissed, through the rest of the write and the settle", async () => {
    // The user said no to this pane. The agent finishing the write it already had open is not a
    // reason to overrule them — including when the settle frame is the first to name the target.
    const dismissed: (boolean | undefined)[] = [];
    sseScript = async function* () {
      yield preview("start");
      useChat.getState().dismissDocument();
      yield preview(" middle");
      dismissed.push(useChat.getState().liveDocument?.dismissed);
      yield toolFrame("running", "start middle end");
      yield toolFrame("ok", "start middle end");
      yield doneFrame();
    };
    await useChat.getState().send("write it", null, async () => {});

    expect(dismissed).toEqual([true]); // a later preview frame doesn't reopen it…
    expect(useChat.getState().liveDocument?.dismissed).toBe(true); // …and neither does the settle
  });

  it("starts a fresh pane for a different module's write", async () => {
    await runTurn([
      preview("knowledge body"),
      preview("notes body", { module: "notes" }),
      doneFrame(),
    ]);

    const live = useChat.getState().liveDocument;
    expect(live?.module).toBe("notes");
    expect(live?.content).toBe("notes body"); // not appended to the other module's document
  });

  it("rebuilds the same body when a dropped stream re-attaches mid-write (#376)", async () => {
    // The core's decision: previews ride the same seq'd buffer as every other frame, so a
    // resume replays the deltas it missed and the concatenation still lands on the full body.
    (api.activeRun as Mock).mockResolvedValue({ run_id: "r1", last_seq: 1 });
    reattachScript = async function* () {
      yield preview("second half", { module: "knowledge", target: "projects/goals.md" }, "2");
      yield toolFrame("ok", "first half second half");
      yield doneFrame();
    };
    sseScript = async function* () {
      yield preview("first half ", { module: "knowledge" }, "1");
      // …and the connection drops: no terminal frame.
    };
    await useChat.getState().send("write it", null, async () => {});

    expect(useChat.getState().liveDocument?.content).toBe("first half second half");
  });

  it("leaves no trace of itself in the turn's activity (ADR-0041)", async () => {
    // A preview is unbounded and ephemeral. It feeds the pane and nothing else — not the live
    // timeline, and (server-side) not the persisted one either.
    await runTurn([
      preview("a document body"),
      toolFrame("running", "a document body"),
      toolFrame("ok", "a document body"),
      {
        event: "awaiting_input",
        data: JSON.stringify({ type: "awaiting_input", run_id: "r1", question: "which one?" }),
      },
    ]);

    expect(useChat.getState().segments).toEqual([
      { kind: "tool", run: { tool: "knowledge_create_document", status: "ok", detail: undefined } },
    ]);
  });
});

describe("The typewriter, on screen (#654)", () => {
  it("renders the body so far, with a caret and no editor", async () => {
    openPane(doc({ content: "# Goals\n\nhalf typed", writing: true, streaming: true }));
    render(<PanelHost />, { wrapper });

    expect(await panel().findByText("half typed")).toBeInTheDocument();
    expect(panel().getByText("writing…")).toBeInTheDocument();
    expect(panel().getByTestId("typewriter-caret")).toBeInTheDocument();
    // Read-only, and not even *asking* what the write did — nothing has been written yet.
    expect(panel().queryByTestId("editor")).not.toBeInTheDocument();
    expect(api.suggestionsEnabled).not.toHaveBeenCalled();
  });

  it("drops the caret the moment the write settles", async () => {
    vi.mocked(api.suggestionsEnabled).mockResolvedValue({ enabled: true });
    openPane(doc({ content: "all typed", writing: false, streaming: false }));
    render(<PanelHost />, { wrapper });

    expect(await panel().findByText("all typed")).toBeInTheDocument();
    expect(panel().queryByTestId("typewriter-caret")).not.toBeInTheDocument();
  });
});
