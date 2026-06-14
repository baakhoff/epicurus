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
  it("lists documents and opens one into the editor", async () => {
    mockModulePage.mockResolvedValue({
      title: "Knowledge",
      docs: [{ id: "a.md", title: "a", path: "a.md" }],
    });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "# Hello" });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));

    const textarea = (await screen.findByLabelText("Edit a.md")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("# Hello");
    expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "a.md");
  });

  it("saves edited content through the core proxy", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "old" });
    mockSave.mockResolvedValue({ path: "a.md", indexed: true, chunk_count: 1 });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    const textarea = await screen.findByLabelText("Edit a.md");

    // Unchanged → save is disabled; editing enables it.
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(textarea, { target: { value: "new body" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith("knowledge", "vault", "a.md", "new body"),
    );
  });

  it("toggles to a rendered preview of the current draft", async () => {
    mockModulePage.mockResolvedValue({ docs: [{ id: "a.md", title: "a", path: "a.md" }] });
    mockModulePageDoc.mockResolvedValue({ path: "a.md", title: "a", content: "# Hi" });
    render(<EditorView module="knowledge" pageId="vault" />, { wrapper });

    fireEvent.click(await screen.findByText("a"));
    await screen.findByLabelText("Edit a.md");
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    expect(screen.getByTestId("preview")).toHaveTextContent("# Hi");
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
    // After a create-save the editor re-syncs the now-saved note from the server.
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
    // Opens the document with no click — the deep link selected it.
    const textarea = (await screen.findByLabelText("Edit a.md")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("# Deep");
    expect(mockModulePageDoc).toHaveBeenCalledWith("knowledge", "vault", "a.md");
  });
});
