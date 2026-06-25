import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SuggestionsScreen } from "@/screens/SuggestionsScreen";

// Stub the review overlay so these tests focus on the inbox (grouping, counts, toggle) rather
// than the modal's per-hunk internals — which have their own tests.
vi.mock("@/components/SuggestionReviewModal", () => ({
  SuggestionReviewModal: ({
    suggestion,
    onClose,
  }: {
    suggestion: { id: string };
    onClose: () => void;
  }) => (
    <div role="dialog">
      reviewing {suggestion.id}
      <button onClick={onClose}>close</button>
    </div>
  ),
}));

const mockModules = vi.fn();
const mockSuggestions = vi.fn();
const mockEnabled = vi.fn();
const mockSetEnabled = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modules: () => mockModules(),
    suggestions: () => mockSuggestions(),
    suggestionsEnabled: (m: string) => mockEnabled(m),
    setSuggestionsEnabled: (m: string, e: boolean) => mockSetEnabled(m, e),
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

function mod(
  name: string,
  pages: Array<{ id: string; title: string; archetype: string; nav_order?: number }>,
  icon = "puzzle",
) {
  return {
    manifest: {
      name,
      version: "1.0.0",
      ui: { icon },
      pages: pages.map((p) => ({ icon: "puzzle", nav_order: 100, ...p })),
    },
    status: { healthy: true },
    enabled: true,
  };
}

function sugg(over: Record<string, unknown>) {
  return {
    id: "s",
    title: "t",
    path: "a.md",
    operation: "update",
    origin: "agent",
    note: "",
    created_at: "2026-06-24T10:30:00Z",
    to_path: "",
    diff: "",
    current: "",
    content: "",
    module: "knowledge",
    page_id: "review",
    ...over,
  };
}

const KNOWLEDGE = mod(
  "knowledge",
  [
    { id: "vault", title: "Knowledge", archetype: "editor", nav_order: 30 },
    { id: "review", title: "Suggestions", archetype: "review", nav_order: 31 },
  ],
  "book",
);
const NOTES = mod("notes", [{ id: "notes", title: "Notes", archetype: "editor" }], "pencil");

beforeEach(() => {
  mockModules.mockReset();
  mockSuggestions.mockReset();
  mockEnabled.mockReset();
  mockSetEnabled.mockReset();
  mockEnabled.mockResolvedValue({ enabled: true });
  mockSetEnabled.mockResolvedValue({ enabled: false });
});

describe("Suggestions inbox", () => {
  it("groups pending suggestions under their module, with a count", async () => {
    mockModules.mockResolvedValue([KNOWLEDGE, NOTES]);
    mockSuggestions.mockResolvedValue([
      sugg({ id: "s1", path: "alpha.md" }),
      sugg({ id: "s2", path: "beta.md", operation: "create" }),
    ]);

    render(<SuggestionsScreen />, { wrapper });

    expect(await screen.findByRole("heading", { name: "Knowledge" })).toBeInTheDocument();
    expect(screen.getByText("alpha.md")).toBeInTheDocument();
    expect(screen.getByText("beta.md")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument(); // the per-module count badge
    // Notes declares no review page, so it is not a group in the inbox.
    expect(screen.queryByRole("heading", { name: "Notes" })).toBeNull();
  });

  it("opens the review overlay for a suggestion", async () => {
    mockModules.mockResolvedValue([KNOWLEDGE]);
    mockSuggestions.mockResolvedValue([sugg({ id: "s9", path: "x.md" })]);

    render(<SuggestionsScreen />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("reviewing s9");
  });

  it("shows each module's review toggle even when it has nothing pending", async () => {
    mockModules.mockResolvedValue([KNOWLEDGE]);
    mockSuggestions.mockResolvedValue([]);

    render(<SuggestionsScreen />, { wrapper });

    expect(await screen.findByText(/nothing pending/i)).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: /review knowledge changes/i });
    await waitFor(() => expect(toggle).toBeEnabled()); // enabled once the toggle's state loads
    fireEvent.click(toggle); // on by default → turning it off
    await waitFor(() => expect(mockSetEnabled).toHaveBeenCalledWith("knowledge", false));
  });

  it("shows an empty state when no module proposes changes", async () => {
    mockModules.mockResolvedValue([NOTES]); // editor-only, no review page anywhere
    mockSuggestions.mockResolvedValue([]);

    render(<SuggestionsScreen />, { wrapper });

    expect(await screen.findByText(/nothing proposes changes yet/i)).toBeInTheDocument();
  });
});
