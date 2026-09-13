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
const mockRecallDimension = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    models: () => mockModels(),
    llmPrefs: () => mockLlmPrefs(),
    setGlobalEmbedDefault: (m: string | null) => mockSetEmbed(m),
    reembed: () => mockReembed(),
    savedModels: () => mockSavedModels(),
    modelSettings: (m: string) => mockModelSettings(m),
    recallDimension: () => mockRecallDimension(),
  },
}));

const HEALTHY_RECALL = { status: "ok", stored_dim: null, expected_dim: null, detail: "" };

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
  mockRecallDimension.mockResolvedValue(HEALTHY_RECALL);
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
    // And the help text warns that the saved list cannot tell chat models from embedding ones.
    expect(screen.getByText(/chat model will fail at embed time/i)).toBeInTheDocument();
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

// ── Re-embed results: started / refused / failed are three states, not two (#848, #860) ────

describe("EmbedDefault — a refused re-embed", () => {
  async function clickReembed() {
    fireEvent.click(await screen.findByRole("button", { name: /re-embed everything/i }));
  }

  it("renders a refusal as a refusal, with its reason and the recovery", async () => {
    // A module refuses when the source it would rebuild from reads empty — the mass de-index
    // fuse (#848). That is the one signal saying "your data is intact and your mount is not";
    // rendering it as "failed to start" and dropping the reason inverted its meaning.
    mockReembed.mockResolvedValue({
      modules: [
        {
          module: "knowledge",
          status: "refused",
          reason: "vault reads empty while the ledger holds 412 documents",
        },
      ],
    });
    render(<EmbedDefault />, { wrapper });

    await clickReembed();

    expect(await screen.findByText(/refused — nothing was rebuilt/)).toBeInTheDocument();
    expect(screen.getByText(/vault reads empty/)).toBeInTheDocument();
    expect(screen.getByText(/vectors are untouched/)).toBeInTheDocument();
    expect(screen.queryByText(/failed to start/)).not.toBeInTheDocument();
  });

  it("still renders a real failure as a failure", async () => {
    mockReembed.mockResolvedValue({ modules: [{ module: "notes", status: "error" }] });
    render(<EmbedDefault />, { wrapper });

    await clickReembed();

    expect(await screen.findByText(/failed to start/)).toBeInTheDocument();
    expect(screen.queryByText(/refused/)).not.toBeInTheDocument();
  });

  it("survives a refusal that carries no reason", async () => {
    mockReembed.mockResolvedValue({ modules: [{ module: "knowledge", status: "refused" }] });
    render(<EmbedDefault />, { wrapper });

    await clickReembed();

    expect(await screen.findByText(/refused — nothing was rebuilt/)).toBeInTheDocument();
    expect(screen.getByText(/vectors are untouched/)).toBeInTheDocument();
  });
});

// ── The recall store's vector width (#944, ADR-0141) ───────────────────────────────────────

describe("EmbedDefault — cross-chat memory's vector width", () => {
  it("says nothing when the recall store is healthy", async () => {
    render(<EmbedDefault />, { wrapper });

    await screen.findByRole("button", { name: /re-embed everything/i });
    await waitFor(() => expect(mockRecallDimension).toHaveBeenCalled());
    expect(screen.queryByTestId("recall-dimension-warning")).not.toBeInTheDocument();
  });

  it("names a stuck width beside the action that fixes it", async () => {
    mockRecallDimension.mockResolvedValue({
      status: "changed",
      stored_dim: 768,
      expected_dim: 4096,
      detail: "embedding dimension changed 768→4096; recall memory needs a rebuild",
    });
    render(<EmbedDefault />, { wrapper });

    const warning = await screen.findByTestId("recall-dimension-warning");
    expect(warning).toHaveTextContent("768→4096");
    expect(warning).toHaveTextContent(/cross-chat memory/i);
  });

  it("names a vector configuration it could not read at all", async () => {
    mockRecallDimension.mockResolvedValue({
      status: "unreadable",
      stored_dim: null,
      expected_dim: 4096,
      detail: "the recall collection's vector configuration could not be read as a single width",
    });
    render(<EmbedDefault />, { wrapper });

    expect(await screen.findByTestId("recall-dimension-warning")).toHaveTextContent(
      /could not be read/,
    );
  });

  it("does not nag about a change it already healed", async () => {
    mockRecallDimension.mockResolvedValue({
      status: "healed",
      stored_dim: 768,
      expected_dim: 4096,
      detail: "recall memory was rebuilt from 768-d to 4096-d vectors",
    });
    render(<EmbedDefault />, { wrapper });

    await screen.findByRole("button", { name: /re-embed everything/i });
    await waitFor(() => expect(mockRecallDimension).toHaveBeenCalled());
    expect(screen.queryByTestId("recall-dimension-warning")).not.toBeInTheDocument();
  });

  it("re-reads the state after a re-embed — the action that clears it", async () => {
    mockRecallDimension
      .mockResolvedValueOnce({
        status: "changed",
        stored_dim: 768,
        expected_dim: 4096,
        detail: "embedding dimension changed 768→4096; recall memory needs a rebuild",
      })
      .mockResolvedValue(HEALTHY_RECALL);
    render(<EmbedDefault />, { wrapper });
    await screen.findByTestId("recall-dimension-warning");

    fireEvent.click(screen.getByRole("button", { name: /re-embed everything/i }));

    await waitFor(() =>
      expect(screen.queryByTestId("recall-dimension-warning")).not.toBeInTheDocument(),
    );
  });
});
