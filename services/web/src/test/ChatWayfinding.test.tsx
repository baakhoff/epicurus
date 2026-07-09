import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Chat wayfinding (#480): the header names the open conversation, starter chips seed the
// composer from installed modules, assistant turns can be copied, and scrolling up
// surfaces a jump-to-latest affordance.
const mockSessions = vi.fn();
const mockModules = vi.fn();
const mockMessages = vi.fn();

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    detail = "";
  },
  api: {
    // One installed model, so the screen shows the normal empty state (starter chips
    // included) rather than the no-model first-run Welcome card.
    models: vi.fn().mockResolvedValue([
      { name: "qwen3:14b", size: 1, loaded: true, hidden: false, capabilities: [] },
    ]),
    providers: vi.fn().mockResolvedValue([]),
    sessionMessages: () => mockMessages(),
    suggestions: vi.fn().mockResolvedValue([]),
    activeRun: vi.fn().mockResolvedValue(null),
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    modelDetails: vi.fn().mockResolvedValue({ capabilities: [] }),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    activeRuns: vi.fn().mockResolvedValue({ session_ids: [] }),
    sessions: () => mockSessions(),
    modules: () => mockModules(),
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
import { useChat } from "@/stores/chat";
import { usePrefs } from "@/stores/prefs";

const moduleSnapshot = (name: string) => ({
  manifest: {
    name,
    version: "1.0.0",
    description: "",
    contract_version: "0.1",
    tags: [],
    tools: [],
    events_emitted: [],
    events_consumed: [],
    config: [],
    secrets: [],
    ui: null,
    pages: [],
    resolver: false,
    attachable: false,
    required_models: [],
  },
  status: { healthy: true, version: "1.0.0" },
  enabled: true,
  disabled_tools: [],
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
  mockSessions.mockReset().mockResolvedValue([]);
  mockModules.mockReset().mockResolvedValue([]);
  mockMessages.mockReset().mockResolvedValue([]);
  usePrefs.setState({ model: null });
  useChat.setState({
    sessionId: "current",
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
});

describe("Chat header title (#480)", () => {
  it("names the open conversation once its title is known", async () => {
    mockSessions.mockResolvedValue([
      { id: "current", title: "Trip planning", message_count: 4, last_at: new Date() },
    ]);
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByRole("heading", { name: "Trip planning" })).toBeInTheDocument();
  });

  it("falls back to a New-conversation placeholder", async () => {
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByRole("heading", { name: "New conversation" })).toBeInTheDocument();
  });
});

describe("Starter prompts (#480)", () => {
  it("offers chips only for installed, healthy, enabled modules", async () => {
    mockModules.mockResolvedValue([
      moduleSnapshot("calendar"),
      { ...moduleSnapshot("mail"), enabled: false },
      { ...moduleSnapshot("tasks"), status: { healthy: false, version: null } },
    ]);
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByText("What's on this week?")).toBeInTheDocument();
    expect(screen.queryByText("Anything important in mail?")).toBeNull();
    expect(screen.queryByText("Plan my day")).toBeNull();
  });

  it("caps the chips at four", async () => {
    mockModules.mockResolvedValue(
      ["calendar", "mail", "tasks", "knowledge", "notes", "websearch"].map(moduleSnapshot),
    );
    const { container } = render(<ChatScreen />, { wrapper });
    await screen.findByText("What's on this week?");
    const chips = [...container.querySelectorAll("button")].filter((b) =>
      ["What's on this week?", "Anything important in mail?", "Plan my day", "Ask my knowledge base", "Capture a note", "Search the web"].includes(b.textContent ?? ""),
    );
    expect(chips).toHaveLength(4);
  });

  it("fills the composer without sending", async () => {
    mockModules.mockResolvedValue([moduleSnapshot("calendar")]);
    render(<ChatScreen />, { wrapper });
    fireEvent.click(await screen.findByText("What's on this week?"));
    expect(useChat.getState().draft).toBe("What's on my calendar this week?");
    expect(useChat.getState().pendingUser).toBeNull(); // nothing was sent
  });
});

describe("Copy an assistant turn (#480)", () => {
  it("copies the message text to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    mockMessages.mockResolvedValue([
      { role: "user", content: "hi", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "assistant", content: "The answer.", created_at: new Date(), attachments: [], entity_refs: [] },
    ]);
    render(<ChatScreen />, { wrapper });
    fireEvent.click(await screen.findByLabelText("Copy message"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("The answer."));
  });

  it("offers copy on every assistant turn, not only the last", async () => {
    mockMessages.mockResolvedValue([
      { role: "user", content: "a", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "assistant", content: "one", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "user", content: "b", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "assistant", content: "two", created_at: new Date(), attachments: [], entity_refs: [] },
    ]);
    render(<ChatScreen />, { wrapper });
    await screen.findByText("two");
    expect(screen.getAllByLabelText("Copy message")).toHaveLength(2);
  });

  // #572: the row wrapper used to carry an unnamed `group`, which an unnamed `group-hover`
  // anywhere below it (e.g. an entity chip's hover-card) would also match — hovering the row
  // revealed every nested chip's card, not just the one under the pointer. Naming the row's
  // scope is half the fix (EntityRef.test.tsx pins the chip side); this pins the row side and
  // confirms the rename didn't just remove the reveal.
  it("names the message row's hover group instead of leaving it unnamed (#572)", async () => {
    mockMessages.mockResolvedValue([
      { role: "user", content: "a", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "assistant", content: "one", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "user", content: "b", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "assistant", content: "two", created_at: new Date(), attachments: [], entity_refs: [] },
    ]);
    const { container } = render(<ChatScreen />, { wrapper });
    await screen.findByText("two");

    // No unnamed `group` scope survives in the transcript.
    expect(container.getElementsByClassName("group")).toHaveLength(0);
    expect(container.getElementsByClassName("group/msg").length).toBeGreaterThanOrEqual(2);

    // Copy-on-hover still works for earlier turns — the row group was renamed, not removed.
    const [earlierCopy, latestCopy] = screen.getAllByLabelText("Copy message");
    expect(earlierCopy.className).toContain("group-hover/msg:opacity-100");
    expect(latestCopy.className).not.toContain("group-hover/msg:opacity-100");
  });
});

describe("Jump to latest (#480)", () => {
  it("appears when the reader scrolls up and re-pins on click", async () => {
    mockMessages.mockResolvedValue([
      { role: "user", content: "hi", created_at: new Date(), attachments: [], entity_refs: [] },
      { role: "assistant", content: "yo", created_at: new Date(), attachments: [], entity_refs: [] },
    ]);
    const { container } = render(<ChatScreen />, { wrapper });
    await screen.findByText("yo");
    const scroller = container.querySelector(".overflow-y-auto") as HTMLElement;
    // jsdom has no layout — model a tall transcript scrolled away from the tail.
    Object.defineProperty(scroller, "scrollHeight", { value: 2000, configurable: true });
    Object.defineProperty(scroller, "clientHeight", { value: 600, configurable: true });
    scroller.scrollTop = 100;
    fireEvent.scroll(scroller);
    const jump = await screen.findByLabelText("Jump to latest");

    fireEvent.click(jump);
    expect(scroller.scrollTop).toBe(2000); // snapped to the tail
    await waitFor(() => expect(screen.queryByLabelText("Jump to latest")).toBeNull());
  });
});
