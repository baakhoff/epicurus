import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EmbedDefault } from "@/screens/ModelsScreen";

const mockModels = vi.fn();
const mockLlmPrefs = vi.fn();
const mockSetEmbed = vi.fn();
const mockReembed = vi.fn();
const mockSavedModels = vi.fn();
const mockModelSettings = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    models: () => mockModels(),
    llmPrefs: () => mockLlmPrefs(),
    setGlobalEmbedDefault: (m: string | null) => mockSetEmbed(m),
    reembed: () => mockReembed(),
    savedModels: () => mockSavedModels(),
    modelSettings: (m: string) => mockModelSettings(m),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockModels.mockResolvedValue([
    { name: "nomic-embed-text", hidden: false, loaded: false, capabilities: [] },
  ]);
  mockLlmPrefs.mockResolvedValue({ global_embed_default: "nomic-embed-text", hidden: [] });
  mockSetEmbed.mockResolvedValue({ status: "ok" });
  mockReembed.mockResolvedValue({
    modules: [
      { module: "knowledge", status: "started" },
      { module: "notes", status: "started" },
    ],
  });
  mockSavedModels.mockResolvedValue([
    {
      model: "openrouter/openai/text-embedding-3-small",
      provider: "openrouter",
      context_length: null,
      capabilities: ["tools"],
      override: { vision: "auto", context_length: null },
    },
  ]);
  mockModelSettings.mockResolvedValue({ context_window: null, keep_alive: null, device: null });
});

describe("EmbedDefault", () => {
  it("re-embeds everything and lists per-module status (#332)", async () => {
    render(<EmbedDefault />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /re-embed everything/i }));

    await waitFor(() => expect(mockReembed).toHaveBeenCalled());
    // Each fanned-out module shows up with its started status.
    expect(await screen.findByText("knowledge")).toBeInTheDocument();
    expect(screen.getByText("notes")).toBeInTheDocument();
    expect(screen.getAllByText(/started/i).length).toBeGreaterThan(0);
  });

  it("notes when there are no embedding-backed modules to re-embed", async () => {
    mockReembed.mockResolvedValue({ modules: [] });
    render(<EmbedDefault />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /re-embed everything/i }));

    expect(await screen.findByText(/no embedding-backed modules/i)).toBeInTheDocument();
  });

  it("offers saved hosted models alongside the local ones (#865)", async () => {
    render(<EmbedDefault />, { wrapper });

    // The hosted group carries the full two-slash OpenRouter id, unshortened.
    const hosted = await screen.findByRole("option", {
      name: "openrouter/openai/text-embedding-3-small",
    });
    expect(hosted).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "nomic-embed-text" })).toBeInTheDocument();
    // And the help text now describes the filtering the role field makes possible (#944),
    // instead of asking the operator to remember which of their saved ids is which.
    expect(screen.getByText(/aren't offered here/i)).toBeInTheDocument();
  });

  it("saves a hosted id as the global embedding default", async () => {
    render(<EmbedDefault />, { wrapper });

    fireEvent.change(await screen.findByLabelText(/global embedding model/i), {
      target: { value: "openrouter/openai/text-embedding-3-small" },
    });

    await waitFor(() =>
      expect(mockSetEmbed).toHaveBeenCalledWith("openrouter/openai/text-embedding-3-small"),
    );
  });

  it("opens the hosted settings sheet — no Ollama runtime options — for a hosted default", async () => {
    mockLlmPrefs.mockResolvedValue({
      global_embed_default: "openrouter/openai/text-embedding-3-small",
      hidden: [],
    });
    render(<EmbedDefault />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /settings for openrouter/i }));

    expect(await screen.findByText(/hosted model settings/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/keep.?alive/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/run on/i)).not.toBeInTheDocument();
  });

  it("keeps showing a stored default that neither list offers", async () => {
    mockLlmPrefs.mockResolvedValue({ global_embed_default: "bge-m3", hidden: [] });
    render(<EmbedDefault />, { wrapper });

    // A local model since deleted is still what the core embeds with; the select must say so
    // rather than silently reading "System default".
    expect(await screen.findByRole("option", { name: "bge-m3" })).toBeInTheDocument();
  });
});

// ── the role filter (#944, ADR-0140) ─────────────────────────────────────────

describe("EmbedDefault role filtering", () => {
  const AUTO = {
    vision: "auto",
    tools: "auto",
    role: "auto",
    context_length: null,
    tools_learned: null,
  } as const;

  it("does not offer a saved model the catalogue knows is a chat model", async () => {
    mockSavedModels.mockResolvedValue([
      {
        model: "claude/claude-sonnet-4-6",
        provider: "claude",
        capabilities: ["tools"],
        role: "chat",
        override: AUTO,
      },
      {
        model: "openrouter/openai/text-embedding-3-small",
        provider: "openrouter",
        capabilities: ["embedding"],
        role: "embedding",
        override: AUTO,
      },
    ]);
    render(<EmbedDefault />, { wrapper });

    expect(
      await screen.findByRole("option", { name: "openrouter/openai/text-embedding-3-small" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "claude/claude-sonnet-4-6" })).toBeNull();
  });

  it("still offers a model of unknown role — a thin catalogue is not a verdict", async () => {
    mockSavedModels.mockResolvedValue([
      {
        model: "openrouter/brand/new-embedder",
        provider: "openrouter",
        capabilities: ["tools"],
        role: "unknown",
        override: AUTO,
      },
    ]);
    render(<EmbedDefault />, { wrapper });
    expect(
      await screen.findByRole("option", { name: "openrouter/brand/new-embedder" }),
    ).toBeInTheDocument();
  });

  it("keeps a stored chat-model choice selectable and says it will fail", async () => {
    // Set before the gate existed, or through another surface: the core really is using it, so
    // hiding it would misreport the state. Naming it is the fix.
    mockLlmPrefs.mockResolvedValue({
      global_embed_default: "claude/claude-sonnet-4-6",
      hidden: [],
    });
    mockSavedModels.mockResolvedValue([
      {
        model: "claude/claude-sonnet-4-6",
        provider: "claude",
        capabilities: ["tools"],
        role: "chat",
        override: AUTO,
      },
    ]);
    render(<EmbedDefault />, { wrapper });

    expect(
      await screen.findByRole("option", { name: "claude/claude-sonnet-4-6" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/embedding with it will fail/i)).toBeInTheDocument();
  });
});
