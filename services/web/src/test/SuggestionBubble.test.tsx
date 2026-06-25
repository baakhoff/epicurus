import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PendingSuggestion } from "@/lib/contracts";
import { SuggestionBubble } from "@/screens/ChatScreen";

const mockSuggestions = vi.fn();
const mockApprove = vi.fn();
const mockReject = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    suggestions: (...a: unknown[]) => mockSuggestions(...a),
    approveSuggestion: (...a: unknown[]) => mockApprove(...a),
    rejectSuggestion: (...a: unknown[]) => mockReject(...a),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const MKPROJECT: PendingSuggestion = {
  module: "knowledge",
  page_id: "vault",
  id: "s1",
  title: "Research",
  operation: "mkproject",
  origin: "agent",
  path: "Research",
  note: "",
  created_at: "2026-06-25T00:00:00Z",
  diff: "",
  to_path: "",
  current: "",
  content: "",
};

beforeEach(() => {
  mockSuggestions.mockReset();
  mockApprove.mockReset();
  mockReject.mockReset();
});

describe("SuggestionBubble — reject without opening (#341)", () => {
  it("rejects a knowledge-base proposal inline, never opening the review overlay", async () => {
    mockSuggestions.mockResolvedValue([MKPROJECT]);
    mockReject.mockResolvedValue(undefined);
    render(<SuggestionBubble />, { wrapper });

    // The bubble surfaces the folder/knowledge-base proposal with an inline Reject.
    expect(await screen.findByText(/add a knowledge base/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(mockReject).toHaveBeenCalledWith("knowledge", "vault", "s1"));
    // Reject discards server-side — it does not approve and does not open a review dialog.
    expect(mockApprove).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders nothing when there are no pending suggestions", async () => {
    mockSuggestions.mockResolvedValue([]);
    const { container } = render(<SuggestionBubble />, { wrapper });
    await waitFor(() => expect(mockSuggestions).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
