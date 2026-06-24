import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModelSettingsSheet } from "@/screens/ModelsScreen";

const mockModelSettings = vi.fn();
const mockModelDetails = vi.fn();
const mockSetModelSettings = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modelSettings: (m: string) => mockModelSettings(m),
    modelDetails: (m: string) => mockModelDetails(m),
    setModelSettings: (m: string, s: unknown) => mockSetModelSettings(m, s),
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
  mockSetModelSettings.mockResolvedValue({ status: "ok" });
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
      }),
    );
  });

  it("renders nothing when no model is selected", () => {
    mockModelSettings.mockResolvedValue({ context_window: null, keep_alive: null });
    const { container } = render(<ModelSettingsSheet model={null} onClose={() => {}} />, {
      wrapper,
    });
    expect(container).toBeEmptyDOMElement();
  });
});
