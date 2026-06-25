import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EmbedDefault } from "@/screens/ModelsScreen";

const mockModels = vi.fn();
const mockLlmPrefs = vi.fn();
const mockSetEmbed = vi.fn();
const mockReembed = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    models: () => mockModels(),
    llmPrefs: () => mockLlmPrefs(),
    setGlobalEmbedDefault: (m: string | null) => mockSetEmbed(m),
    reembed: () => mockReembed(),
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
});
