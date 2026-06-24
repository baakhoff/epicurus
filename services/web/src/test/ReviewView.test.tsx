import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewView } from "@/components/archetypes/ReviewView";

const mockModulePage = vi.fn();
const mockEnabled = vi.fn();
const mockSetEnabled = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    modulePage: (...a: unknown[]) => mockModulePage(...a),
    suggestionsEnabled: (...a: unknown[]) => mockEnabled(...a),
    setSuggestionsEnabled: (...a: unknown[]) => mockSetEnabled(...a),
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
