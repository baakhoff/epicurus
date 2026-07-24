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
    // Sources for the automation model picker (#667); only fetched for an automation suggestion.
    models: () => Promise.resolve([{ name: "llama-3.3", hidden: false }]),
    savedModels: () => Promise.resolve([{ model: "gpt-4o" }]),
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

  it("approves an append with the merged content", async () => {
    render(
      <SuggestionReviewModal
        suggestion={suggestion({ operation: "append", current: "a\n", content: "a\nb\n" })}
        onClose={() => {}}
      />,
      { wrapper },
    );
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

  // ADR-0090: the operator can hand-edit the draft directly, not just tick/untick hunks.
  it("editing the draft directly overrides the hunk-merged content", async () => {
    render(
      <SuggestionReviewModal
        suggestion={suggestion({ current: "a\nb\n", content: "a\nB\n" })}
        onClose={() => {}}
      />,
      { wrapper },
    );
    const draft = screen.getByLabelText(/editable draft/i);
    fireEvent.change(draft, { target: { value: "a\nhand-typed\n" } });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(mockApprove).toHaveBeenCalledWith("knowledge", "vault", "s1", "a\nhand-typed\n"),
    );
  });

  it("a manual edit survives further hunk toggling", async () => {
    render(
      <SuggestionReviewModal
        suggestion={suggestion({ current: "a\nb\n", content: "a\nB\n" })}
        onClose={() => {}}
      />,
      { wrapper },
    );
    const draft = screen.getByLabelText(/editable draft/i);
    fireEvent.change(draft, { target: { value: "hand-typed\n" } });
    fireEvent.click(screen.getByLabelText(/apply change 1/i)); // untick — would normally re-merge
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(mockApprove).toHaveBeenCalledWith("knowledge", "vault", "s1", "hand-typed\n"),
    );
  });
});

// An automation proposal (#667/ADR-0107) renders a structured preview + a model picker rather
// than a text diff; approve carries the operator's model choice as the content.
function automationSuggestion(overrides: Partial<PendingSuggestion> = {}): PendingSuggestion {
  return suggestion({
    module: "core",
    page_id: "automations",
    operation: "create",
    path: "automation/new",
    title: "Important mail alerts",
    automation: {
      name: "Important mail alerts",
      trigger: "When mail emits mail.received",
      filter: "importance = 'high'",
      action: "Tell me about important mail.",
      autonomy: "notify",
      autonomy_label: "Notify — look, don't touch",
      sinks: ["push"],
      model: null,
    },
    ...overrides,
  });
}

describe("SuggestionReviewModal — automations (#667)", () => {
  it("renders the trigger/action preview, not an editable diff", () => {
    render(<SuggestionReviewModal suggestion={automationSuggestion()} onClose={() => {}} />, {
      wrapper,
    });
    expect(screen.getByText("When mail emits mail.received")).toBeInTheDocument();
    expect(screen.getByText(/Tell me about important mail/)).toBeInTheDocument();
    expect(screen.getByText("Notify — look, don't touch")).toBeInTheDocument();
    // No document draft editor — the automation path replaces it.
    expect(screen.queryByLabelText(/editable draft/i)).not.toBeInTheDocument();
  });

  it("approves with the operator's picked model", async () => {
    render(<SuggestionReviewModal suggestion={automationSuggestion()} onClose={() => {}} />, {
      wrapper,
    });
    // The picker is populated from api.models / api.savedModels.
    await screen.findByRole("option", { name: "llama-3.3" });
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), {
      target: { value: "llama-3.3" },
    });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(mockApprove).toHaveBeenCalledWith("core", "automations", "s1", "llama-3.3"),
    );
  });

  it("approves with an empty model (operator default) when the picker is untouched", async () => {
    render(<SuggestionReviewModal suggestion={automationSuggestion()} onClose={() => {}} />, {
      wrapper,
    });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() =>
      expect(mockApprove).toHaveBeenCalledWith("core", "automations", "s1", ""),
    );
  });
});
