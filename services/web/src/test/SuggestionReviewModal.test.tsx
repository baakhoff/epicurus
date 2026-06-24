import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SuggestionReviewModal } from "@/components/SuggestionReviewModal";
import type { PendingSuggestion } from "@/lib/contracts";

const mockApprove = vi.fn();
const mockReject = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    approveSuggestion: (...a: unknown[]) => mockApprove(...a),
    rejectSuggestion: (...a: unknown[]) => mockReject(...a),
  },
}));
vi.mock("@/components/Markdown", () => ({
  Markdown: ({ children }: { children: string }) => <div>{children}</div>,
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function suggestion(overrides: Partial<PendingSuggestion>): PendingSuggestion {
  return {
    id: "s1",
    title: "a",
    path: "kb/a.md",
    operation: "update",
    origin: "agent",
    note: "",
    created_at: "2026-06-24T00:00:00Z",
    diff: "",
    to_path: "",
    current: "",
    content: "",
    module: "knowledge",
    page_id: "vault",
    ...overrides,
  };
}

beforeEach(() => {
  mockApprove.mockReset().mockResolvedValue({});
  mockReject.mockReset().mockResolvedValue({});
});

describe("SuggestionReviewModal", () => {
  it("approves an edit with the full merged content (all hunks accepted)", async () => {
    render(
      <SuggestionReviewModal
        suggestion={suggestion({ current: "a\nb\n", content: "a\nB\n" })}
        onClose={() => {}}
      />,
      { wrapper },
    );
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(mockApprove).toHaveBeenCalledWith("knowledge", "vault", "s1", "a\nB\n"),
    );
  });

  it("unticking a hunk approves only the accepted change", async () => {
    render(
      <SuggestionReviewModal
        suggestion={suggestion({ current: "a\nb\n", content: "a\nB\n" })}
        onClose={() => {}}
      />,
      { wrapper },
    );
    fireEvent.click(screen.getByLabelText(/apply change 1/i)); // untick the only change
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(mockApprove).toHaveBeenCalledWith("knowledge", "vault", "s1", "a\nb\n"),
    );
  });

  it("confirms a move from → to and approves without content", async () => {
    render(
      <SuggestionReviewModal
        suggestion={suggestion({ operation: "move", path: "kb/a.md", to_path: "kb/b.md" })}
        onClose={() => {}}
      />,
      { wrapper },
    );
    expect(screen.getByText("kb/b.md")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(mockApprove).toHaveBeenCalledWith("knowledge", "vault", "s1", undefined),
    );
  });

  it("rejects through the overlay", async () => {
    render(<SuggestionReviewModal suggestion={suggestion({})} onClose={() => {}} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    await waitFor(() => expect(mockReject).toHaveBeenCalledWith("knowledge", "vault", "s1"));
  });

  it("Ignore closes without calling the server", () => {
    const onClose = vi.fn();
    render(<SuggestionReviewModal suggestion={suggestion({})} onClose={onClose} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /ignore/i }));
    expect(onClose).toHaveBeenCalled();
    expect(mockApprove).not.toHaveBeenCalled();
    expect(mockReject).not.toHaveBeenCalled();
  });
});
