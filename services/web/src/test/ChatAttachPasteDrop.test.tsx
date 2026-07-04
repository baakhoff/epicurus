import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { ChatScreen } from "@/screens/ChatScreen";
import { useChat } from "@/stores/chat";
import { useToasts } from "@/stores/toasts";

// Paste & drag-drop attachments (#489): both routes reuse the AttachMenu upload path
// (`api.uploadAttachment`), so the server's 413/415 messaging stays single-sourced —
// surfaced here as a toast. The mock keeps the real ApiError class (the failure path
// does an instanceof check) and stubs only the api surface.

const mockUpload = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      models: vi.fn().mockResolvedValue([]),
      providers: vi.fn().mockResolvedValue([]),
      sessions: vi.fn().mockResolvedValue([]),
      sessionMessages: vi.fn().mockResolvedValue([]),
      suggestions: vi.fn().mockResolvedValue([]),
      modules: vi.fn().mockResolvedValue([]),
      deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
      activeRun: vi.fn().mockResolvedValue(null),
      cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
      llmPrefs: vi.fn().mockResolvedValue({
        global_default: null,
        global_embed_default: null,
        global_context_window: null,
        hidden: [],
      }),
      uploadAttachment: (file: File) => mockUpload(file),
    },
  };
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
  mockUpload.mockReset();
  useToasts.setState({ toasts: [] });
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

const png = () => new File(["x"], "shot.png", { type: "image/png" });

describe("paste to attach", () => {
  it("uploads pasted files and lands the same pill the picker produces", async () => {
    mockUpload.mockResolvedValue({ att_id: "a1", kind: "image", title: "shot.png" });
    render(<ChatScreen />, { wrapper });
    const textarea = await screen.findByLabelText("Message");

    fireEvent.paste(textarea, { clipboardData: { files: [png()] } });

    expect(mockUpload).toHaveBeenCalledTimes(1);
    // The real pill (with its remove affordance) appears once the server answers.
    expect(await screen.findByLabelText("Remove shot.png")).toBeInTheDocument();
  });

  it("leaves text-only pastes to the browser and never calls the upload endpoint", async () => {
    render(<ChatScreen />, { wrapper });
    const textarea = await screen.findByLabelText("Message");

    fireEvent.paste(textarea, { clipboardData: { files: [] } });

    expect(mockUpload).not.toHaveBeenCalled();
  });

  it("shows a spinner pill while the upload is in flight", async () => {
    mockUpload.mockReturnValue(new Promise(() => {})); // never settles
    render(<ChatScreen />, { wrapper });
    const textarea = await screen.findByLabelText("Message");

    fireEvent.paste(textarea, { clipboardData: { files: [png()] } });

    // The placeholder carries the filename but no remove affordance yet.
    expect(await screen.findByText("shot.png")).toBeInTheDocument();
    expect(screen.queryByLabelText("Remove shot.png")).not.toBeInTheDocument();
  });

  it("surfaces an upload failure as a toast carrying the server's message", async () => {
    mockUpload.mockRejectedValue(new ApiError(413, "File too large (max 10 MB)."));
    render(<ChatScreen />, { wrapper });
    const textarea = await screen.findByLabelText("Message");

    fireEvent.paste(textarea, { clipboardData: { files: [png()] } });

    await waitFor(() => {
      const toasts = useToasts.getState().toasts;
      expect(toasts).toHaveLength(1);
      expect(toasts[0].tone).toBe("error");
      expect(toasts[0].message).toBe("File too large (max 10 MB).");
    });
    // The spinner pill is gone; nothing attached.
    expect(screen.queryByText("shot.png")).not.toBeInTheDocument();
  });
});

describe("drag-drop to attach", () => {
  it("shows the themed hint only for file drags, and clears it on leave", async () => {
    const { container } = render(<ChatScreen />, { wrapper });
    await screen.findByLabelText("Message");
    const root = container.firstElementChild as HTMLElement;

    // A text-selection drag must not trigger the overlay.
    fireEvent.dragEnter(root, { dataTransfer: { types: ["text/plain"], files: [] } });
    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();

    fireEvent.dragEnter(root, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.getByText("Drop to attach")).toBeInTheDocument();

    fireEvent.dragLeave(root, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
  });

  it("keeps the hint up across nested enter/leave pairs (depth counter)", async () => {
    const { container } = render(<ChatScreen />, { wrapper });
    const composer = await screen.findByLabelText("Message");
    const root = container.firstElementChild as HTMLElement;

    // Crossing into a child fires enter(child) before leave(parent) — the overlay
    // must not flicker off in between.
    fireEvent.dragEnter(root, { dataTransfer: { types: ["Files"], files: [] } });
    fireEvent.dragEnter(composer, { dataTransfer: { types: ["Files"], files: [] } });
    fireEvent.dragLeave(root, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.getByText("Drop to attach")).toBeInTheDocument();

    fireEvent.dragLeave(composer, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
  });

  it("uploads dropped files and dismisses the hint", async () => {
    mockUpload.mockResolvedValue({ att_id: "a2", kind: "file", title: "notes.pdf" });
    const { container } = render(<ChatScreen />, { wrapper });
    await screen.findByLabelText("Message");
    const root = container.firstElementChild as HTMLElement;

    const pdf = new File(["y"], "notes.pdf", { type: "application/pdf" });
    fireEvent.dragEnter(root, { dataTransfer: { types: ["Files"], files: [] } });
    fireEvent.drop(root, { dataTransfer: { types: ["Files"], files: [pdf] } });

    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(await screen.findByLabelText("Remove notes.pdf")).toBeInTheDocument();
  });

  it("ignores drops that carry no files", async () => {
    const { container } = render(<ChatScreen />, { wrapper });
    await screen.findByLabelText("Message");
    const root = container.firstElementChild as HTMLElement;

    fireEvent.drop(root, { dataTransfer: { types: ["text/plain"], files: [] } });

    expect(mockUpload).not.toHaveBeenCalled();
  });

  it("uploads every file of a multi-file drop", async () => {
    mockUpload
      .mockResolvedValueOnce({ att_id: "m1", kind: "image", title: "one.png" })
      .mockResolvedValueOnce({ att_id: "m2", kind: "image", title: "two.png" });
    const { container } = render(<ChatScreen />, { wrapper });
    await screen.findByLabelText("Message");
    const root = container.firstElementChild as HTMLElement;

    const files = [new File(["1"], "one.png"), new File(["2"], "two.png")];
    fireEvent.drop(root, { dataTransfer: { types: ["Files"], files } });

    expect(mockUpload).toHaveBeenCalledTimes(2);
    expect(await screen.findByLabelText("Remove one.png")).toBeInTheDocument();
    expect(await screen.findByLabelText("Remove two.png")).toBeInTheDocument();
  });
});
