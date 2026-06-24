import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditorView } from "@/components/archetypes/EditorView";

const mockModulePage = vi.fn();
const mockModulePageDoc = vi.fn();
const mockSave = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    modulePageDoc: (...args: unknown[]) => mockModulePageDoc(...args),
    saveModulePageDoc: (...args: unknown[]) => mockSave(...args),
  },
}));

// Keep this a focused unit test: stub the shared prose renderer.
vi.mock("@/components/Markdown", () => ({
  Markdown: ({ children }: { children: string }) => <div data-testid="preview">{children}</div>,
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
  mockModulePage.mockReset();
  mockModulePageDoc.mockReset();
  mockSave.mockReset();
});

describe("EditorView", () => {
  it("lists documents and opens one rendered, then edits via the toggle", async () => {
    mockModulePage.mockResolvedValue({
      title: "Knowledge",
      docs: [{ id: "a.md", title: "a", path: "a.md" }],
    });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "# Hello" });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));

    // A document opens rendered (ADR-0042) — the preview shows, the raw source does not.
    expect(await screen.findByTestId("preview")).toHaveTextContent("# Hello");
    expect(screen.queryByLabelText("Edit a.md")).toBeNull();
    expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "a.md");

    // The Edit toggle drops into the raw source.
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const textarea = (await screen.findByLabelText("Edit a.md")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("# Hello");
  });

  it("saves edited content through the core proxy", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "old" });
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const textarea = await screen.findByLabelText("Edit a.md");

    // Unchanged → save is disabled; editing enables it.
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(textarea, { target: { value: "new body" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("knowledge", "vault", "a.md", "new body"),
    );
  });

  it("saves after the document idles, with no Save click (ADR-0042)", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "old" });
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    render(<EditorView module="notes" pageId="notes" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const textarea = await screen.findByLabelText("Edit a.md");

    // Edit under fake timers so the idle timeout is fake, then let it elapse — no Save click.
    vi.useFakeTimers();
    try {
      fireEvent.change(textarea, { target: { value: "idle body" } });
      await vi.advanceTimersByTimeAsync(4500); // > IDLE_SAVE_MS
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("notes", "notes", "a.md", "idle body"),
    );
  });

  it("saves the open document when you switch away — to its own path, never the new one", async () => {
    mockModulePage.mockResolvedValue({
      docs: [
        { id: "a.md", title: "a", path: "a.md" },
        { id: "b.md", title: "b", path: "b.md" },
      ],
    });
    // A resolves; B never does — we sit in the window where the buffer still holds A.
    mockModulePageDoc.mockImplementation((_m: string, _p: string, path: string) =>
      path === "a.md"
        ? Promise.resolve({ path: "a.md", title: "a", content: "AAA" })
        : new Promise(() => {}),
    );
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(await screen.findByLabelText("Edit a.md"), {
      target: { value: "AAA-edited" },
    });

    // Switching documents is "leaving" — it flushes the buffer to *its* path (A), and the
    // stale-path guard means A's draft never lands on the not-yet-loaded B.
    fireEvent.click(screen.getByText("b"));
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("knowledge", "vault", "a.md", "AAA-edited"),
    );
    expect(mockSave).not.toHaveBeenCalledWith("knowledge", "vault", "b.md", "AAA-edited");
  });

  it("saves the open document on unmount (leaving the editor)", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "old" });
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    const { unmount } = render(<EditorView module="notes" pageId="notes" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(await screen.findByLabelText("Edit a.md"), { target: { value: "leaving" } });

    unmount(); // navigating away from the editor screen
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("notes", "notes", "a.md", "leaving"),
    );
  });

  it("renders read-only when the vault is externally owned (#232)", async () => {
    mockModulePage.mockResolvedValue({
      title: "Knowledge",
      docs: [{ id: "a.md", title: "a", path: "a.md" }],
      can_manage_files: false,
      read_only: true,
    });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "# Hello" });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    // A read-only badge + banner make the externally-owned mode legible, and there is
    // no Save path in either view.
    expect(await screen.findByText("read-only")).toBeInTheDocument();
    expect(screen.getByText(/managed externally/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    // The raw source is shown but not editable.
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const textarea = (await screen.findByLabelText("Edit a.md")) as HTMLTextAreaElement;
    expect(textarea.readOnly).toBe(true);
  });

  it("toggles between the rendered preview and the raw source", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "# Hi" });
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    // Default is the rendered preview.
    expect(await screen.findByTestId("preview")).toHaveTextContent("# Hi");

    // Edit → raw source; type; Preview reflects the new draft.
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const textarea = await screen.findByLabelText("Edit a.md");
    fireEvent.change(textarea, { target: { value: "# Bye" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(screen.getByTestId("preview")).toHaveTextContent("# Bye");
  });

  it("shows an empty-vault hint when there are no documents", async () => {
    mockModulePage.mockResolvedValue({ docs: [] });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    expect(await screen.findByText(/empty vault/i)).toBeInTheDocument();
  });

  it("shows the New note control only when the page is authorable", async () => {
    mockModulePage.mockResolvedValue({ docs: [], can_create: false });
    const { unmount } = render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    await screen.findByText(/empty vault/i);
    expect(screen.queryByRole("button", { name: /new note/i })).toBeNull();
    unmount();

    mockModulePage.mockResolvedValue({ docs: [], can_create: true });
    render(<EditorView module="notes" pageId="notes" />, { wrapper });
    expect(await screen.findByRole("button", { name: /new note/i })).toBeInTheDocument();
    expect(screen.getByText(/no notes yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/empty vault/i)).toBeNull();
  });

  it("creates a note: seeds an H1 title and saves to a fresh slug", async () => {
    mockModulePage.mockResolvedValue({ title: "Notes", docs: [], can_create: true });
    mockSave.mockResolvedValue({ path: "my-idea", indexed: true, chunk_count: 1 });
    // After a create-save the now-saved note may be fetched, but the local buffer is
    // authoritative (seeded by path) so the fetch never clobbers in-flight edits.
    mockModulePageDoc.mockResolvedValue({
      path: "my-idea",
      title: "My Idea",
      content: "# My Idea\n\n",
    });
    render(<EditorView module="notes" pageId="notes" />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /new note/i }));
    fireEvent.change(screen.getByLabelText("New note title"), { target: { value: "My Idea" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    const textarea = (await screen.findByLabelText("Edit my-idea")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("# My Idea\n\n");
    // A brand-new note never fetches the (absent) document.
    expect(mockModulePageDoc).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("notes", "notes", "my-idea", "# My Idea\n\n"),
    );
  });

  it("disambiguates the new slug against an existing note", async () => {
    mockModulePage.mockResolvedValue({
      title: "Notes",
      docs: [{ id: "my-idea", title: "My Idea", path: "my-idea" }],
      can_create: true,
    });
    render(<EditorView module="notes" pageId="notes" />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /new note/i }));
    fireEvent.change(screen.getByLabelText("New note title"), { target: { value: "My Idea" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByLabelText("Edit my-idea-2")).toBeInTheDocument();
  });

  it("deep-links to the document named by the ?doc= param", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "# Deep" });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/m/knowledge/vault?doc=a.md"]}>
          <EditorView module="knowledge" pageId="vault" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // Opens the document rendered, with no click — the deep link selected it.
    expect(await screen.findByTestId("preview")).toHaveTextContent("# Deep");
    expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "a.md");
  });
});
