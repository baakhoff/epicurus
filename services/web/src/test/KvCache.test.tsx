import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { KvCache } from "@/screens/ModelsScreen";

const mockLlmPrefs = vi.fn();
const mockSetKvCacheType = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    llmPrefs: () => mockLlmPrefs(),
    setKvCacheType: (v: string | null) => mockSetKvCacheType(v),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const PREFS = {
  global_default: null,
  global_embed_default: null,
  global_context_window: null,
  kv_cache_type: null,
  global_agent_max_steps: null,
  hidden: [],
};

beforeEach(() => {
  mockLlmPrefs.mockReset();
  mockSetKvCacheType.mockReset();
  mockLlmPrefs.mockResolvedValue(PREFS);
});

describe("KvCache", () => {
  it("confirms it applied (Ollama restarted) when the core could restart", async () => {
    mockSetKvCacheType.mockResolvedValue({ status: "ok", applied: true });
    render(<KvCache />, { wrapper });
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "q8_0" } });
    expect(await screen.findByText(/Ollama restarted/)).toBeInTheDocument();
    expect(mockSetKvCacheType).toHaveBeenCalledWith("q8_0");
  });

  it("falls back to manual-restart instructions when Docker isn't wired", async () => {
    mockSetKvCacheType.mockResolvedValue({ status: "ok", applied: false });
    render(<KvCache />, { wrapper });
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "q4_0" } });
    expect(await screen.findByText(/no Docker access/)).toBeInTheDocument();
    expect(screen.getByText("OLLAMA_FLASH_ATTENTION=1")).toBeInTheDocument();
  });
});
