import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SavedHostedModels } from "@/screens/ModelsScreen";

const mockSavedModels = vi.fn();
const mockLlmPrefs = vi.fn();
const mockSetGlobalDefault = vi.fn();
const mockRemoveSavedModel = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    savedModels: () => mockSavedModels(),
    llmPrefs: () => mockLlmPrefs(),
    setGlobalDefault: (m: string | null) => mockSetGlobalDefault(m),
    removeSavedModel: (m: string) => mockRemoveSavedModel(m),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockSavedModels.mockResolvedValue([
    { model: "claude/claude-3-5-sonnet-latest", provider: "claude" },
    { model: "gpt/gpt-4o", provider: "gpt" },
  ]);
  mockLlmPrefs.mockResolvedValue({ global_default: "gpt/gpt-4o", hidden: [] });
  mockSetGlobalDefault.mockResolvedValue({ status: "ok" });
  mockRemoveSavedModel.mockResolvedValue({ status: "ok" });
});

describe("SavedHostedModels", () => {
  it("lists saved models grouped under their provider label", async () => {
    render(<SavedHostedModels />, { wrapper });
    expect(await screen.findByText("claude/claude-3-5-sonnet-latest")).toBeInTheDocument();
    expect(screen.getByText("gpt/gpt-4o")).toBeInTheDocument();
    // Grouped under readable provider names, not the raw alias.
    expect(screen.getByText("Anthropic Claude")).toBeInTheDocument();
    expect(screen.getByText("OpenAI")).toBeInTheDocument();
  });

  it("shows a compact context-window chip when the model reports one (#618)", async () => {
    mockSavedModels.mockResolvedValue([
      { model: "claude/claude-3-7-sonnet-20250219", provider: "claude", context_length: 200000 },
      { model: "gpt/some-unlisted-model", provider: "gpt", context_length: null },
    ]);
    render(<SavedHostedModels />, { wrapper });
    expect(await screen.findByText("200k")).toBeInTheDocument();
    await screen.findByText("gpt/some-unlisted-model"); // the row rendered
    expect(screen.queryByText(/^\d.*[kM]$/)).toHaveTextContent("200k"); // the only chip present
  });

  it("marks the current global default and lets another be starred", async () => {
    render(<SavedHostedModels />, { wrapper });
    // gpt/gpt-4o is the stored default → its row carries the badge.
    expect(await screen.findByText("default")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /set claude\/claude-3-5-sonnet-latest as default/i }),
    );
    await waitFor(() =>
      expect(mockSetGlobalDefault).toHaveBeenCalledWith("claude/claude-3-5-sonnet-latest"),
    );
  });

  it("removes a saved model", async () => {
    render(<SavedHostedModels />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Remove gpt/gpt-4o" }));
    await waitFor(() => expect(mockRemoveSavedModel).toHaveBeenCalledWith("gpt/gpt-4o"));
  });

  it("shows an empty hint when nothing is saved", async () => {
    mockSavedModels.mockResolvedValue([]);
    render(<SavedHostedModels />, { wrapper });
    expect(await screen.findByText(/pick a hosted model in a chat/i)).toBeInTheDocument();
  });
});
