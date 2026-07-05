import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EditorView } from "@/components/archetypes/EditorView";

const mockModulePage = vi.fn();
const mockModulePageDoc = vi.fn();
const mockSave = vi.fn();
const mockCreateProject = vi.fn();
const mockDeleteProject = vi.fn();
const mockVersions = vi.fn();
const mockVersion = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    modulePageDoc: (...args: unknown[]) => mockModulePageDoc(...args),
    saveModulePageDoc: (...args: unknown[]) => mockSave(...args),
    createModuleProject: (...args: unknown[]) => mockCreateProject(...args),
    deleteModuleProject: (...args: unknown[]) => mockDeleteProject(...args),
    modulePageDocVersions: (...args: unknown[]) => mockVersions(...args),
    modulePageDocVersion: (...args: unknown[]) => mockVersion(...args),
  },
}));

// Keep this a focused unit test: stub the shared prose renderer.
vi.mock("@/components/Markdown", () => ({
  Markdown: ({ children }: { children: string }) => <div data-testid="preview">{children}</div>,
}));

// The editable Preview (#377) is the heavy Milkdown WYSIWYG — stub it so this stays a focused
// unit test (no ProseMirror in jsdom). The stub mirrors the contract: it shows `value` and
// reports edits via `onChange`, so the buffer/save wiring can be exercised.
vi.mock("@/components/archetypes/WysiwygEditor", () => ({
  default: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <textarea
      data-testid="wysiwyg"
      aria-label="wysiwyg editor"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
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
  mockCreateProject.mockReset();
  mockDeleteProject.mockReset();
  mockVersions.mockReset();
  mockVersion.mockReset();
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

    // A document opens rendered & editable (ADR-0042, #377) — the WYSIWYG shows, raw does not.
    expect(await screen.findByTestId("wysiwyg")).toHaveValue("# Hello");
    expect(screen.queryByLabelText("Edit a.md")).toBeNull();
    expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "a.md");

    // The Edit toggle drops into the raw source.
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const textarea = (await screen.findByLabelText("Edit a.md")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("# Hello");
  });

  it("edits the WYSIWYG preview and saves the markdown through the core (#377)", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "# Hello" });
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    render(<EditorView module="notes" pageId="notes" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    // Opens editable in the WYSIWYG — not the read-only renderer.
    const wys = await screen.findByTestId("wysiwyg");
    expect(wys).toHaveValue("# Hello");
    // Unchanged → Save disabled; editing in the WYSIWYG marks dirty and saves the markdown.
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(wys, { target: { value: "# Hello world" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("notes", "notes", "a.md", "# Hello world"),
    );
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
    // Default is the editable rendered preview (WYSIWYG, #377).
    expect(await screen.findByTestId("wysiwyg")).toHaveValue("# Hi");

    // Edit → raw source; type; Preview reflects the new draft.
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const textarea = await screen.findByLabelText("Edit a.md");
    fireEvent.change(textarea, { target: { value: "# Bye" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByTestId("wysiwyg")).toHaveValue("# Bye");
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
    expect(await screen.findByTestId("wysiwyg")).toHaveValue("# Deep");
    expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "a.md");
  });

  it("browses, views, and restores a past version (ADR-0046)", async () => {
    mockModulePage.mockResolvedValue({
      docs: [{ id: "a.md", title: "a", path: "a.md" }],
      versioned: true,
    });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "current" });
    mockVersions.mockResolvedValue({
      versions: [
        { version_id: "2", created_at: "2020-01-02T10:00:00.000Z", title: "a", size: 99 },
        { version_id: "1", created_at: "2020-01-01T10:00:00.000Z", title: "a", size: 3 },
      ],
    });
    mockVersion.mockResolvedValue({
      path: "a.md",
      version_id: "1",
      created_at: "2020-01-01T10:00:00.000Z",
      title: "a",
      content: "the old version",
    });
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    render(<EditorView module="notes" pageId="notes" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    // The History button appears only because the page is `versioned`; it opens the dropdown.
    fireEvent.click(await screen.findByRole("button", { name: "Version history" }));
    // View the older snapshot (size 3) read-only.
    fireEvent.click(await screen.findByText("3 ch"));
    expect(await screen.findByTestId("preview")).toHaveTextContent("the old version");
    expect(mockVersion).toHaveBeenCalledWith("notes", "notes", "a.md", "1");

    // Restore brings that version back as a fresh save through the normal path.
    fireEvent.click(screen.getByRole("button", { name: "Restore this version" }));
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("notes", "notes", "a.md", "the old version"),
    );
  });

  it("hides version history when the page is not versioned", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "x" });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    fireEvent.click(await screen.findByText("a"));
    await screen.findByTestId("wysiwyg");
    expect(screen.queryByRole("button", { name: "Version history" })).toBeNull();
  });
});

// ── projects / knowledge-base scopes (#KB-refactor) ──────────────────────────

const SCOPED = {
  title: "Knowledge",
  docs: [{ id: "alpha.md", title: "alpha", path: "alpha.md", type: "file" as const }],
  scopes: [
    { id: "kb", title: "kb", kind: "project" as const },
    { id: "work", title: "work", kind: "project" as const },
    { id: "__docs__", title: "Platform docs", kind: "reference" as const },
  ],
  scope: "kb",
  scope_noun: "knowledge base",
  can_manage_files: true,
  can_create_scope: true,
};

describe("EditorView — knowledge bases (scopes)", () => {
  it("shows the switcher and prefixes the active scope onto document paths", async () => {
    mockModulePage.mockResolvedValue(SCOPED);
    mockModulePageDoc.mockResolvedValue({ path: "kb/alpha.md", title: "alpha", content: "# A" });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    // The switcher shows the active knowledge base.
    expect(await screen.findByRole("button", { name: "kb" })).toBeInTheDocument();
    fireEvent.click(await screen.findByText("alpha"));
    await waitFor(() =>
      expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "kb/alpha.md"),
    );
  });

  it("refetches with the scope param when another knowledge base is selected", async () => {
    mockModulePage.mockResolvedValue(SCOPED);
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "kb" })); // open the switcher
    fireEvent.click(await screen.findByText("work")); // pick another knowledge base
    await waitFor(() =>
      expect(mockModulePage).toHaveBeenCalledWith("knowledge", "vault", { scope: "work" }),
    );
  });

  it("New document creates at the scope root and saves with the scope prefix", async () => {
    mockModulePage.mockResolvedValue(SCOPED);
    mockSave.mockResolvedValue({ path: "kb/new-note.md", indexed: true, chunk_count: 0 });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: /new document/i }));
    const ta = await screen.findByLabelText("Edit new-note.md");
    fireEvent.change(ta, { target: { value: "# Fresh" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("knowledge", "vault", "kb/new-note.md", "# Fresh"),
    );
  });

  it("creates a knowledge base via the switcher", async () => {
    mockModulePage.mockResolvedValue(SCOPED);
    mockCreateProject.mockResolvedValue({ id: "research", title: "research", kind: "project" });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "kb" })); // open the switcher
    fireEvent.click(await screen.findByRole("button", { name: /new knowledge base/i }));
    fireEvent.change(screen.getByLabelText("New knowledge base name"), {
      target: { value: "Research" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(mockCreateProject).toHaveBeenCalledWith("knowledge", "vault", "Research"),
    );
  });

  it("deletes a knowledge base via the switcher after confirming (#340)", async () => {
    mockModulePage.mockResolvedValue(SCOPED);
    mockDeleteProject.mockResolvedValue(undefined);
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "kb" })); // open the switcher
    // Each project row carries a delete affordance; remove the non-active "work" base.
    fireEvent.click(await screen.findByRole("button", { name: "Delete work" }));
    // The confirm dialog gates the destructive call; confirming invokes the delete.
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(mockDeleteProject).toHaveBeenCalledWith("knowledge", "vault", "work"),
    );
  });

  it("filters the document tree by the search box (#339)", async () => {
    mockModulePage.mockResolvedValue({
      ...SCOPED,
      docs: [
        { id: "alpha.md", title: "alpha", path: "alpha.md", type: "file" as const },
        { id: "beta.md", title: "beta", path: "beta.md", type: "file" as const },
      ],
    });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    expect(await screen.findByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("beta")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search documents"), { target: { value: "beta" } });
    // Only the matching document remains; the non-match is filtered out.
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
    expect(screen.getByText("beta")).toBeInTheDocument();
  });

  it("starts the New-note flow from a ?new=1 deep-link (#491)", async () => {
    mockModulePage.mockResolvedValue({
      can_create: true,
      docs: [{ id: "a.md", title: "a", path: "a.md" }],
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/m/notes/notes?new=1"]}>
          <EditorView module="notes" pageId="notes" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // The naming form is already open — exactly as if "New note" had been pressed.
    expect(await screen.findByLabelText("New note title")).toBeInTheDocument();
  });

  it("leaves the New-note flow closed without the deep-link", async () => {
    mockModulePage.mockResolvedValue({
      can_create: true,
      docs: [{ id: "a.md", title: "a", path: "a.md" }],
    });
    render(<EditorView module="notes" pageId="notes" />, { wrapper });
    expect(await screen.findByRole("button", { name: /New note/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("New note title")).toBeNull();
  });

  it("renders the platform-docs reference scope as read-only", async () => {
    mockModulePage.mockResolvedValue({
      ...SCOPED,
      scope: "__docs__",
      read_only: true,
      can_manage_files: false,
      docs: [{ id: "index.md", title: "index", path: "index.md", type: "file" as const }],
    });
    mockModulePageDoc.mockResolvedValue({
      path: "__docs__/index.md",
      title: "index",
      content: "# Docs",
    });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });
    fireEvent.click(await screen.findByText("index"));
    // The doc opens rendered (ADR-0042); the read-only-reference banner shows immediately.
    expect(await screen.findByText(/read-only reference/i)).toBeInTheDocument();
    expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "__docs__/index.md");
    // The raw source is shown via the Edit toggle but is not editable.
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const ta = (await screen.findByLabelText("Edit index.md")) as HTMLTextAreaElement;
    expect(ta.readOnly).toBe(true);
  });
});
