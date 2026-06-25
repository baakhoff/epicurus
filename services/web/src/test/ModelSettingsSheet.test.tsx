import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModelSettingsSheet } from "@/screens/ModelsScreen";

const mockModelSettings = vi.fn();
const mockModelDetails = vi.fn();
const mockSetModelSettings = vi.fn();
const mockLlmPrefs = vi.fn();
const mockSystemInfo = vi.fn();
const mockModelVariants = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modelSettings: (m: string) => mockModelSettings(m),
    modelDetails: (m: string) => mockModelDetails(m),
    setModelSettings: (m: string, s: unknown) => mockSetModelSettings(m, s),
    // The form resolves the effective context from these two (per-model → global → suggested).
    llmPrefs: () => mockLlmPrefs(),
    systemInfo: () => mockSystemInfo(),
    // The quant-variant pick-list (#330).
    modelVariants: (m: string) => mockModelVariants(m),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockModelSettings.mockReset();
  mockModelDetails.mockReset();
  mockSetModelSettings.mockReset();
  mockLlmPrefs.mockReset();
  mockSystemInfo.mockReset();
  mockSetModelSettings.mockResolvedValue({ status: "ok" });
  mockLlmPrefs.mockResolvedValue({ global_context_window: null });
  mockSystemInfo.mockResolvedValue({ suggested_context: { min: 2048, suggested: 16384, max: 24000 } });
  mockModelVariants.mockResolvedValue({ model: "llama3.2:latest", variants: [] });
  mockModelDetails.mockResolvedValue({
    quantization: "Q4_K_M",
    parameter_size: "8.0B",
    context_length: 131072,
    family: "llama",
  });
});

describe("ModelSettingsSheet", () => {
  it("shows read-only details and seeds the form from stored settings", async () => {
    mockModelSettings.mockResolvedValue({ context_window: 8192, keep_alive: "30m" });

    render(<ModelSettingsSheet model="llama3.2:latest" onClose={() => {}} />, { wrapper });

    // Quantization shows in both the badge row and the read-only quant section.
    expect((await screen.findAllByText("Q4_K_M")).length).toBeGreaterThan(0);
    expect(screen.getByText(/trained 131,072 ctx/)).toBeInTheDocument();
    const ctx = (await screen.findByLabelText(
      "Per-model context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx.value).toBe("8192"));
    expect((screen.getByLabelText("Keep-alive") as HTMLInputElement).value).toBe("30m");
  });

  it("saves the edited context window and keep-alive", async () => {
    mockModelSettings.mockResolvedValue({ context_window: 8192, keep_alive: "30m" });
    const onClose = vi.fn();

    render(<ModelSettingsSheet model="llama3.2:latest" onClose={onClose} />, { wrapper });

    const ctx = (await screen.findByLabelText(
      "Per-model context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx.value).toBe("8192"));
    fireEvent.change(ctx, { target: { value: "4096" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSetModelSettings).toHaveBeenCalledWith("llama3.2:latest", {
        context_window: 4096,
        keep_alive: "30m",
        device: null,
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("treats a blank context window as inherit (null)", async () => {
    mockModelSettings.mockResolvedValue({ context_window: 8192, keep_alive: null });

    render(<ModelSettingsSheet model="llama3.2:latest" onClose={() => {}} />, { wrapper });

    const ctx = (await screen.findByLabelText(
      "Per-model context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx.value).toBe("8192"));
    fireEvent.change(ctx, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSetModelSettings).toHaveBeenCalledWith("llama3.2:latest", {
        context_window: null,
        keep_alive: null,
        device: null,
      }),
    );
  });

  it("saves the chosen run-on device", async () => {
    mockModelSettings.mockResolvedValue({ context_window: 8192, keep_alive: null, device: null });

    render(<ModelSettingsSheet model="llama3.2:latest" onClose={() => {}} />, { wrapper });

    // Wait until the form has seeded (so a click isn't clobbered by the seed), then pick CPU.
    const ctx = (await screen.findByLabelText(
      "Per-model context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx.value).toBe("8192"));
    fireEvent.click(screen.getByRole("button", { name: "CPU" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSetModelSettings).toHaveBeenCalledWith("llama3.2:latest", {
        context_window: 8192,
        keep_alive: null,
        device: "cpu",
      }),
    );
  });

  it("shows the resolved context it inherits when no per-model value is set", async () => {
    mockModelSettings.mockResolvedValue({ context_window: null, keep_alive: null, device: null });
    mockLlmPrefs.mockResolvedValue({ global_context_window: 8000 });

    render(<ModelSettingsSheet model="llama3.2:latest" onClose={() => {}} />, { wrapper });

    // The field is blank (inherit) but the read-out spells out the effective number + its source,
    // and the field's placeholder echoes it — no more guessing what's actually in play (#328).
    const ctx = (await screen.findByLabelText(
      "Per-model context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx).toHaveAttribute("placeholder", "8000"));
    expect(ctx.value).toBe("");
    expect(await screen.findByText(/tokens from the global default/i)).toHaveTextContent(
      /Inherits 8,000 tokens from the global default/i,
    );
  });

  it("reads out the per-model value once one is set", async () => {
    mockModelSettings.mockResolvedValue({ context_window: 8192, keep_alive: null, device: null });

    render(<ModelSettingsSheet model="llama3.2:latest" onClose={() => {}} />, { wrapper });

    expect(await screen.findByText(/this model will use/i)).toHaveTextContent(
      /This model will use 8,192 tokens/i,
    );
  });

  it("lists the model's available quant variants from the registry (#330)", async () => {
    mockModelVariants.mockResolvedValue({
      model: "llama3.2:latest",
      variants: [{ tag: "llama3.2:3b-instruct-q8_0", quant: "q8_0" }],
    });

    render(<ModelSettingsSheet model="llama3.2:latest" onClose={() => {}} />, { wrapper });

    expect(await screen.findByText("q8_0")).toBeInTheDocument();
    // The tag line also carries the size estimate, so match it loosely.
    expect(screen.getByText(/llama3\.2:3b-instruct-q8_0/)).toBeInTheDocument();
  });

  it("renders nothing when no model is selected", () => {
    mockModelSettings.mockResolvedValue({ context_window: null, keep_alive: null });
    const { container } = render(<ModelSettingsSheet model={null} onClose={() => {}} />, {
      wrapper,
    });
    expect(container).toBeEmptyDOMElement();
  });
});
