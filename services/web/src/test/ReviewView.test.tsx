import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewView } from "@/components/archetypes/ReviewView";

const mockModulePage = vi.fn();
const mockEnabled = vi.fn();
const mockSetEnabled = vi.fn();
const mockReviewAudit = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    modulePage: (...a: unknown[]) => mockModulePage(...a),
    suggestionsEnabled: (...a: unknown[]) => mockEnabled(...a),
    setSuggestionsEnabled: (...a: unknown[]) => mockSetEnabled(...a),
    reviewAudit: (...a: unknown[]) => mockReviewAudit(...a),
    approveSuggestion: vi.fn(),
    rejectSuggestion: vi.fn(),
  },
}));
vi.mock("@/components/Markdown", () => ({
  Markdown: ({ children }: { children: string }) => <div>{children}</div>,
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockModulePage.mockReset();
  mockEnabled.mockReset();
  mockSetEnabled.mockReset();
  mockReviewAudit.mockReset().mockResolvedValue({ decisions: [] });
});

describe("ReviewView — suggestions toggle (#KB-refactor)", () => {
  it("renders the review toggle and turning it off calls the api", async () => {
    mockModulePage.mockResolvedValue({ title: "Suggestions", suggestions: [] });
    mockEnabled.mockResolvedValue({ enabled: true });
    mockSetEnabled.mockResolvedValue({});
    render(<ReviewView module="knowledge" pageId="review" />, { wrapper });

    const sw = await screen.findByRole("switch", { name: /review agent changes/i });
    expect(sw).toHaveAttribute("aria-checked", "true");
    fireEvent.click(sw);
    await waitFor(() => expect(mockSetEnabled).toHaveBeenCalledWith("knowledge", false));
  });

  it("shows the auto-apply note when review is off", async () => {
    mockModulePage.mockResolvedValue({ suggestions: [] });
    mockEnabled.mockResolvedValue({ enabled: false });
    render(<ReviewView module="notes" pageId="review" />, { wrapper });

    expect(await screen.findByText(/applied automatically/i)).toBeInTheDocument();
    const sw = await screen.findByRole("switch", { name: /review agent changes/i });
    expect(sw).toHaveAttribute("aria-checked", "false");
  });
});

// ADR-0090: the resolved-decision audit trail under the pending queue.
describe("ReviewView — recently resolved history (ADR-0090)", () => {
  it("shows nothing when there is no resolved history", async () => {
    mockModulePage.mockResolvedValue({ suggestions: [] });
    mockEnabled.mockResolvedValue({ enabled: true });
    mockReviewAudit.mockResolvedValue({ decisions: [] });
    render(<ReviewView module="knowledge" pageId="review" />, { wrapper });

    await screen.findByRole("switch", { name: /review agent changes/i });
    expect(screen.queryByText(/recently resolved/i)).not.toBeInTheDocument();
  });

  it("lists a resolved decision with its outcome", async () => {
    mockModulePage.mockResolvedValue({ suggestions: [] });
    mockEnabled.mockResolvedValue({ enabled: true });
    mockReviewAudit.mockResolvedValue({
      decisions: [
        {
          id: "d1",
          title: "a",
          path: "kb/a.md",
          operation: "update",
          origin: "agent",
          note: "",
          created_at: "2026-06-24T00:00:00Z",
          decided_at: "2026-06-24T01:00:00Z",
          decision: "approved",
          proposed_content: "agent proposal",
          applied_content: "operator edit",
          to_path: "",
        },
      ],
    });
    render(<ReviewView module="knowledge" pageId="review" />, { wrapper });

    expect(await screen.findByText(/recently resolved \(1\)/i)).toBeInTheDocument();
    fireEvent.click(screen.getByText(/recently resolved/i)); // <details> needs opening in jsdom
    expect(screen.getByText("kb/a.md")).toBeInTheDocument();
    expect(screen.getByText("approved")).toBeInTheDocument();

    fireEvent.click(screen.getByText(/see what changed/i));
    expect(screen.getByText(/agent proposal/i)).toBeInTheDocument();
    expect(screen.getByText(/operator edit/i)).toBeInTheDocument();
  });
});
