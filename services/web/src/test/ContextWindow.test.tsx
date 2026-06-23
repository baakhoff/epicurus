import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ContextWindow } from "@/screens/ModelsScreen";

// ── mock the API client ───────────────────────────────────────────────────────

const mockLlmPrefs = vi.fn();
const mockSystemInfo = vi.fn();
const mockSetContextWindow = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    llmPrefs: () => mockLlmPrefs(),
    systemInfo: () => mockSystemInfo(),
    setContextWindow: (value: number | null) => mockSetContextWindow(value),
  },
}));

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const PREFS_UNSET = {
  global_default: "llama3.2",
  global_embed_default: null,
  global_context_window: null,
  hidden: [],
};

const SYSTEM_WITH_GPU = {
  gpu: { vendor: "nvidia", name: "RTX 4090", vram_total_mb: 24564, vram_free_mb: 23000 },
  ram_total_mb: 32000,
  model: { name: "llama3.2:latest", size_mb: 4482 },
  suggested_context: { min: 2048, suggested: 16384, max: 24000 },
};

beforeEach(() => {
  mockLlmPrefs.mockReset();
  mockSystemInfo.mockReset();
  mockSetContextWindow.mockReset();
  mockSetContextWindow.mockResolvedValue({ status: "ok" });
});

describe("ContextWindow card", () => {
  it("shows the detected GPU, the active model, and the suggested range", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS_UNSET);
    mockSystemInfo.mockResolvedValue(SYSTEM_WITH_GPU);

    render(<ContextWindow />, { wrapper });

    expect(await screen.findByText(/RTX 4090/)).toBeInTheDocument();
    expect(screen.getByText(/llama3\.2:latest/)).toBeInTheDocument();
    // The suggested value appears (in the summary strong + the apply button) and the
    // estimate caveat is surfaced so the operator knows it's not a measured maximum.
    expect(screen.getAllByText(/16,384/).length).toBeGreaterThan(0);
    expect(screen.getByText(/rough estimate/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /use suggested/i })).toBeInTheDocument();
  });

  it("falls back to a CPU label when no GPU is detected", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS_UNSET);
    mockSystemInfo.mockResolvedValue({
      gpu: null,
      ram_total_mb: 16000,
      model: { name: "llama3.2", size_mb: 4482 },
      suggested_context: { min: 2048, suggested: 8192, max: 8192 },
    });

    render(<ContextWindow />, { wrapper });

    expect(await screen.findByText(/No GPU detected/i)).toBeInTheDocument();
  });

  it("applies the suggestion via the Use-suggested button", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS_UNSET);
    mockSystemInfo.mockResolvedValue(SYSTEM_WITH_GPU);

    render(<ContextWindow />, { wrapper });

    const useBtn = await screen.findByRole("button", { name: /use suggested \(16,384\)/i });
    fireEvent.click(useBtn);
    await waitFor(() => expect(mockSetContextWindow).toHaveBeenCalledWith(16384));
  });

  it("commits an edited token count to the pref", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS_UNSET);
    mockSystemInfo.mockResolvedValue(SYSTEM_WITH_GPU);

    render(<ContextWindow />, { wrapper });

    const input = await screen.findByRole("spinbutton", { name: /context window tokens/i });
    fireEvent.change(input, { target: { value: "12288" } });
    fireEvent.blur(input);
    await waitFor(() => expect(mockSetContextWindow).toHaveBeenCalledWith(12288));
  });

  it("clears the override with Reset to default when a value is stored", async () => {
    mockLlmPrefs.mockResolvedValue({ ...PREFS_UNSET, global_context_window: 16384 });
    mockSystemInfo.mockResolvedValue(SYSTEM_WITH_GPU);

    render(<ContextWindow />, { wrapper });

    const reset = await screen.findByRole("button", { name: /reset to the system default/i });
    fireEvent.click(reset);
    await waitFor(() => expect(mockSetContextWindow).toHaveBeenCalledWith(null));
  });
});
