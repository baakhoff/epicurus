import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

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

// Streaming isn't exercised here (we assert the controls render, not the re-run).
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

const msg = (id: number, role: string, content: string) => ({
  id,
  role,
  content,
  created_at: new Date(),
  entity_refs: [],
  attachments: [],
});

/** One exchange — the last user message is the only one there is. */
const ONE_EXCHANGE = [msg(1, "user", "first question"), msg(2, "assistant", "an answer")];
/** Two exchanges, so there is history *behind* the last user message to edit into (#552). */
const TWO_EXCHANGES = [
  ...ONE_EXCHANGE,
  msg(3, "user", "second question"),
  msg(4, "assistant", "another answer"),
];

// The chat store is a module-level singleton, so a test that swaps an action has to put the
// real one back or it leaks into every test after it.
const realEditAndRerun = useChat.getState().editAndRerun;

beforeEach(() => {
  vi.mocked(api.sessionMessages).mockResolvedValue(ONE_EXCHANGE as never);
  useChat.setState({
    draft: "",
    streaming: false,
    segments: [],
    pendingUser: null,
    readiness: null,
    error: null,
    paused: false,
    abort: null,
    editAndRerun: realEditAndRerun,
  });
  useConnection.setState({ online: true, coreDown: false });
});

describe("Chat tail controls (#302)", () => {
  it("shows Regenerate on the last answer and Edit on the last user message", async () => {
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByText("an answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate response" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit message" })).toBeInTheDocument();
  });

  it("opens an inline editor seeded with the message when Edit is clicked", async () => {
    render(<ChatScreen />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Edit message" }));
    const editor = (await screen.findByLabelText("Edit message")) as HTMLTextAreaElement;
    expect(editor.value).toBe("first question");
    expect(screen.getByRole("button", { name: "Resend" })).toBeInTheDocument();
    // The Regenerate control hides while editing.
    expect(screen.queryByRole("button", { name: "Regenerate response" })).not.toBeInTheDocument();
  });
});

// Regenerate/Resend are send-adjacent the same way the composer's Send is (#494) — gate them
// on the connection store too, or they fail into the old error card instead of the hint (#530).
describe("Chat tail controls while unreachable (#530)", () => {
  it("disables Regenerate while the core is unreachable", async () => {
    render(<ChatScreen />, { wrapper });
    const regenerate = await screen.findByRole("button", { name: "Regenerate response" });
    expect(regenerate).not.toBeDisabled();

    act(() => useConnection.getState().reportUnreachable());
    expect(regenerate).toBeDisabled();
  });

  it("disables Resend and ignores Enter-to-resend while the core is unreachable", async () => {
    render(<ChatScreen />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Edit message" }));
    const editor = await screen.findByLabelText("Edit message");

    act(() => useConnection.getState().reportUnreachable());
    expect(screen.getByRole("button", { name: "Resend" })).toBeDisabled();

    // Enter-to-resend bypasses the button entirely (composer parity, #494) — the guard
    // inside saveEdit() must catch it too, or the editor would close on a failed resend.
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(screen.getByRole("button", { name: "Resend" })).toBeInTheDocument();
  });
});

// Editing back in the history rewrites the conversation from that point (#552). The three
// things that matter: every user message is editable, the *named* message is the one revised,
// and nothing is discarded before the user has seen the count and agreed.
describe("Editing any user message in history (#552)", () => {
  // Typed to the store's own action, so a change to `editAndRerun`'s signature fails here
  // rather than silently letting the mock drift from what it stands in for.
  let editAndRerun: Mock<typeof realEditAndRerun>;

  beforeEach(() => {
    vi.mocked(api.sessionMessages).mockResolvedValue(TWO_EXCHANGES as never);
    editAndRerun = vi.fn<typeof realEditAndRerun>().mockResolvedValue(undefined);
    useChat.setState({ editAndRerun });
  });

  /** Open the inline editor on the user message at `idx` (0-based over user messages). */
  async function openEditor(idx: number) {
    const buttons = await screen.findAllByRole("button", { name: "Edit message" });
    fireEvent.click(buttons[idx]);
    return (await screen.findByLabelText("Edit message")) as HTMLTextAreaElement;
  }

  it("offers Edit on every user message, not just the last", async () => {
    render(<ChatScreen />, { wrapper });
    expect(await screen.findByText("second question")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Edit message" })).toHaveLength(2);
  });

  it("seeds the editor from the message that was clicked, not the last one", async () => {
    render(<ChatScreen />, { wrapper });
    expect((await openEditor(0)).value).toBe("first question");
  });

  it("confirms with the count of what a mid-history edit discards", async () => {
    render(<ChatScreen />, { wrapper });
    const editor = await openEditor(0);
    fireEvent.change(editor, { target: { value: "reworded" } });
    fireEvent.click(screen.getByRole("button", { name: "Resend" }));

    // The answer, the follow-up ask, and its answer — 3 messages after the edited one.
    expect(await screen.findByRole("alertdialog")).toHaveTextContent(
      "removes the 3 later messages",
    );
    expect(editAndRerun).not.toHaveBeenCalled(); // nothing sent until it's agreed to
  });

  it("cancelling the confirm discards nothing and keeps the draft", async () => {
    render(<ChatScreen />, { wrapper });
    const editor = await openEditor(0);
    fireEvent.change(editor, { target: { value: "reworded" } });
    fireEvent.click(screen.getByRole("button", { name: "Resend" }));
    // The dialog's Cancel, not the inline editor's (both are on screen).
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(editAndRerun).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect((await screen.findByLabelText("Edit message")) as HTMLTextAreaElement).toHaveValue(
      "reworded",
    );
  });

  it("confirming sends the edit against the clicked message's own id", async () => {
    render(<ChatScreen />, { wrapper });
    const editor = await openEditor(0);
    fireEvent.change(editor, { target: { value: "reworded" } });
    fireEvent.click(screen.getByRole("button", { name: "Resend" }));
    // The dialog's Resend, not the inline editor's (both are on screen — #660).
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Resend" }));

    // id 1 — the message actually clicked, not id 3 (the last user turn #302 would have hit).
    expect(editAndRerun).toHaveBeenCalledWith("reworded", null, expect.any(Function), 1);
  });

  // saveEdit() guards on streaming/connectionLost before ever opening this dialog — but the
  // dialog can sit open for a while, and either can change in the meantime (another tab or a
  // scheduled turn starts a run; the connection drops). Confirming must re-check at click time,
  // or it trims the transcript to a state the server was never asked to produce (#660).
  it("blocks the resend if a run started while the dialog was open, instead of trimming past it", async () => {
    render(<ChatScreen />, { wrapper });
    const editor = await openEditor(0);
    fireEvent.change(editor, { target: { value: "reworded" } });
    fireEvent.click(screen.getByRole("button", { name: "Resend" }));
    const dialog = await screen.findByRole("alertdialog");

    act(() => useChat.setState({ streaming: true }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Resend" }));

    expect(editAndRerun).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    // Blocked exactly like Cancel — the inline editor stays open with the draft intact to retry.
    expect((await screen.findByLabelText("Edit message")) as HTMLTextAreaElement).toHaveValue(
      "reworded",
    );
  });

  it("blocks the resend if the connection dropped while the dialog was open", async () => {
    render(<ChatScreen />, { wrapper });
    const editor = await openEditor(0);
    fireEvent.change(editor, { target: { value: "reworded" } });
    fireEvent.click(screen.getByRole("button", { name: "Resend" }));
    const dialog = await screen.findByRole("alertdialog");

    act(() => useConnection.getState().reportUnreachable());
    fireEvent.click(within(dialog).getByRole("button", { name: "Resend" }));

    expect(editAndRerun).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("edits the last user message with no confirm, exactly as before (#302)", async () => {
    render(<ChatScreen />, { wrapper });
    const editor = await openEditor(1);
    fireEvent.change(editor, { target: { value: "corrected" } });
    fireEvent.click(screen.getByRole("button", { name: "Resend" }));

    // Nothing real is lost — only the answer being regenerated — so it goes straight through.
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(editAndRerun).toHaveBeenCalledWith("corrected", null, expect.any(Function), 3);
  });
});
