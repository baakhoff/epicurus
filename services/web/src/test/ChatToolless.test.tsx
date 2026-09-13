import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/screens/ChatScreen";
import { usePrefs } from "@/stores/prefs";

// The chat reads the selected model's details to decide whether to warn it can't call tools.
const mockModelDetails = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    models: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    activeRun: vi.fn().mockResolvedValue(null), // no in-flight run to recover (#376)
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    }),
    modelDetails: (model: string) => mockModelDetails(model),
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

beforeEach(() => {
  mockModelDetails.mockReset();
  // Select a local model for the chat (no provider prefix → treated as local).
  usePrefs.setState({ model: "llama3.2" });
});

afterEach(() => {
  usePrefs.setState({ model: null });
});

describe("Chat tool-capability hint", () => {
  it("warns when the selected local model can't use tools", async () => {
    mockModelDetails.mockResolvedValue({
      capabilities: ["completion", "vision"],
      supports_tools: false,
    });
    render(<ChatScreen />, { wrapper });
    await waitFor(() => expect(screen.getAllByText(/can't use tools/i).length).toBeGreaterThan(0));
    expect(mockModelDetails).toHaveBeenCalledWith("llama3.2");
  });

  it("shows no warning when the model supports tools", async () => {
    mockModelDetails.mockResolvedValue({
      capabilities: ["completion", "tools"],
      supports_tools: true,
    });
    render(<ChatScreen />, { wrapper });
    // Let the details query resolve, then assert the hint is absent.
    await waitFor(() => expect(mockModelDetails).toHaveBeenCalled());
    expect(screen.queryByText(/can't use tools/i)).toBeNull();
  });

  it("shows no warning when capabilities are unknown (empty list)", async () => {
    mockModelDetails.mockResolvedValue({ capabilities: [] });
    render(<ChatScreen />, { wrapper });
    await waitFor(() => expect(mockModelDetails).toHaveBeenCalled());
    expect(screen.queryByText(/can't use tools/i)).toBeNull();
  });

  it("warns for a *hosted* model with no tool support (#947)", async () => {
    // The old rule was `effectiveIsLocal && ...`, so this notice could never fire for a hosted
    // id — which is exactly the case the owner hit. Capabilities are empty here too: a hosted
    // model with no tools and no vision has nothing to badge, and list-emptiness must not be
    // mistaken for "unknown" now that the core answers the question directly.
    usePrefs.setState({ model: "openrouter/some/model" });
    mockModelDetails.mockResolvedValue({ capabilities: [], supports_tools: false });
    render(<ChatScreen />, { wrapper });
    await waitFor(() => expect(screen.getAllByText(/can't use tools/i).length).toBeGreaterThan(0));
    expect(mockModelDetails).toHaveBeenCalledWith("openrouter/some/model");
  });
});
