import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { KvCache } from "@/screens/ModelsScreen";

const mockLlmPrefs = vi.fn();
const mockSetKvCacheType = vi.fn();
const mockSystemInfo = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    llmPrefs: () => mockLlmPrefs(),
    setKvCacheType: (v: string | null) => mockSetKvCacheType(v),
    systemInfo: () => mockSystemInfo(),
  },
}));

// A system with the given VRAM (MB).
function systemWithVram(vramMb: number) {
  return {
    gpu: { vendor: "nvidia", name: "GPU", vram_total_mb: vramMb, vram_free_mb: vramMb },
    ram_total_mb: 32000,
  };
}

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
  mockSystemInfo.mockReset();
  mockLlmPrefs.mockResolvedValue(PREFS);
  // Moderate VRAM by default → the recommender suggests q8_0.
  mockSystemInfo.mockResolvedValue(systemWithVram(12288));
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

  it("suggests a KV-cache type from VRAM and applies it in one click (#329)", async () => {
    mockSystemInfo.mockResolvedValue(systemWithVram(12288)); // moderate → q8_0
    mockSetKvCacheType.mockResolvedValue({ status: "ok", applied: true });
    render(<KvCache />, { wrapper });

    // The hint explains the suggestion (reason text) and offers a one-click apply; the current
    // pick (default f16) differs, so the "Use q8_0" button is shown.
    expect(await screen.findByText(/q8_0 halves the cache/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /use q8_0/i }));
    await waitFor(() => expect(mockSetKvCacheType).toHaveBeenCalledWith("q8_0"));
  });

  it("notes when the current choice already matches the recommendation", async () => {
    mockSystemInfo.mockResolvedValue(systemWithVram(24576)); // ample → f16 (the default)
    render(<KvCache />, { wrapper });

    // f16 is the active default, so there's no apply button — just a confirming note.
    expect(await screen.findByText(/recommended for your hardware/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^use /i })).not.toBeInTheDocument();
  });
});
