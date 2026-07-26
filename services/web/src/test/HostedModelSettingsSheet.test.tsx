import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedModelSettingsSheet } from "@/screens/ModelsScreen";

const mockModelSettings = vi.fn();
const mockSetModelSettings = vi.fn();
const mockSetSavedModelOverride = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modelSettings: (m: string) => mockModelSettings(m),
    setModelSettings: (m: string, s: unknown) => mockSetModelSettings(m, s),
    setSavedModelOverride: (m: string, o: unknown) => mockSetSavedModelOverride(m, o),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const HOSTED = "claude/claude-3-5-sonnet-latest";

beforeEach(() => {
  mockModelSettings.mockReset();
  mockSetModelSettings.mockReset();
  mockSetSavedModelOverride.mockReset();
  mockSetModelSettings.mockResolvedValue({ status: "ok" });
  mockSetSavedModelOverride.mockResolvedValue({ status: "ok" });
});

describe("HostedModelSettingsSheet", () => {
  it("shows only the context field — no keep-alive, run-on, or quantization (#570)", async () => {
    mockModelSettings.mockResolvedValue({ context_window: null, keep_alive: null, device: null });

    render(<HostedModelSettingsSheet model={HOSTED} onClose={() => {}} />, { wrapper });

    expect(await screen.findByLabelText("Hosted context window tokens")).toBeInTheDocument();
    // The local-only runtime controls must not appear for a hosted model.
    expect(screen.queryByLabelText("Keep-alive")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Run on" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Quantization/i)).not.toBeInTheDocument();
    // No "inherit the global default" framing — the Ollama pref never applies to hosted.
    expect(screen.queryByText(/global default/i)).not.toBeInTheDocument();
  });

  it("seeds the field from the stored budget and reads it out", async () => {
    mockModelSettings.mockResolvedValue({ context_window: 50000, keep_alive: null, device: null });

    render(<HostedModelSettingsSheet model={HOSTED} onClose={() => {}} />, { wrapper });

    const ctx = (await screen.findByLabelText(
      "Hosted context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx.value).toBe("50000"));
    // Match the readout's distinctive lead ("Conversations are trimmed to") — the field hint also
    // contains "trimmed to", so a looser regex would match two elements.
    expect(await screen.findByText(/Conversations are trimmed to/i)).toHaveTextContent(
      /50,000 tokens before each request/i,
    );
  });

  it("saves the budget with keep-alive and device forced null", async () => {
    // Seed a known value first so the change isn't clobbered by the one-shot seed (a value typed
    // before the settings query resolves would otherwise be reset), then edit it.
    mockModelSettings.mockResolvedValue({ context_window: 10000, keep_alive: null, device: null });
    const onClose = vi.fn();

    render(<HostedModelSettingsSheet model={HOSTED} onClose={onClose} />, { wrapper });

    const ctx = (await screen.findByLabelText(
      "Hosted context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx.value).toBe("10000"));
    fireEvent.change(ctx, { target: { value: "50000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSetModelSettings).toHaveBeenCalledWith(HOSTED, {
        context_window: 50000,
        keep_alive: null,
        device: null,
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("clears the budget back to null", async () => {
    mockModelSettings.mockResolvedValue({ context_window: 50000, keep_alive: null, device: null });

    render(<HostedModelSettingsSheet model={HOSTED} onClose={() => {}} />, { wrapper });

    const ctx = (await screen.findByLabelText(
      "Hosted context window tokens",
    )) as HTMLInputElement;
    await waitFor(() => expect(ctx.value).toBe("50000"));
    // "Clear budget" shows only when a budget is set; it blanks the field and saves null.
    fireEvent.click(screen.getByRole("button", { name: "Clear budget" }));

    await waitFor(() =>
      expect(mockSetModelSettings).toHaveBeenCalledWith(HOSTED, {
        context_window: null,
        keep_alive: null,
        device: null,
      }),
    );
  });

  it("renders nothing when no model is selected", () => {
    const { container } = render(<HostedModelSettingsSheet model={null} onClose={() => {}} />, {
      wrapper,
    });
    expect(container).toBeEmptyDOMElement();
  });
});

// ── capability override (#711) ────────────────────────────────────────────────

const AUTO = { vision: "auto", context_length: null } as const;

describe("HostedModelSettingsSheet capability override", () => {
  beforeEach(() => {
    mockModelSettings.mockResolvedValue({ context_window: null, keep_alive: null, device: null });
  });

  it("seeds the controls from the stored override", async () => {
    render(
      <HostedModelSettingsSheet
        model={HOSTED}
        override={{ vision: "on", context_length: 256000 }}
        onClose={() => {}}
      />,
      { wrapper },
    );

    const vision = (await screen.findByLabelText("Image input support")) as HTMLSelectElement;
    expect(vision.value).toBe("on");
    const declared = screen.getByLabelText("Declared context length tokens") as HTMLInputElement;
    expect(declared.value).toBe("256000");
  });

  it("defaults to auto when the core sends no override", async () => {
    render(<HostedModelSettingsSheet model={HOSTED} onClose={() => {}} />, { wrapper });

    const vision = (await screen.findByLabelText("Image input support")) as HTMLSelectElement;
    expect(vision.value).toBe("auto");
    expect(screen.getByText(/decided by the provider catalogue/i)).toBeInTheDocument();
  });

  it("saves vision=on alongside the budget", async () => {
    render(<HostedModelSettingsSheet model={HOSTED} override={AUTO} onClose={() => {}} />, {
      wrapper,
    });

    const vision = (await screen.findByLabelText("Image input support")) as HTMLSelectElement;
    fireEvent.change(vision, { target: { value: "on" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSetSavedModelOverride).toHaveBeenCalledWith(HOSTED, {
        vision: "on",
        context_length: null,
      }),
    );
  });

  it("saves a declared context length", async () => {
    render(<HostedModelSettingsSheet model={HOSTED} override={AUTO} onClose={() => {}} />, {
      wrapper,
    });

    const declared = (await screen.findByLabelText(
      "Declared context length tokens",
    )) as HTMLInputElement;
    fireEvent.change(declared, { target: { value: "256000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSetSavedModelOverride).toHaveBeenCalledWith(HOSTED, {
        vision: "auto",
        context_length: 256000,
      }),
    );
  });

  it("clears an override back to auto", async () => {
    render(
      <HostedModelSettingsSheet
        model={HOSTED}
        override={{ vision: "on", context_length: 256000 }}
        onClose={() => {}}
      />,
      { wrapper },
    );

    const vision = (await screen.findByLabelText("Image input support")) as HTMLSelectElement;
    fireEvent.change(vision, { target: { value: "auto" } });
    fireEvent.change(screen.getByLabelText("Declared context length tokens"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockSetSavedModelOverride).toHaveBeenCalledWith(HOSTED, {
        vision: "auto",
        context_length: null,
      }),
    );
  });

  it("does not write the override when it wasn't touched", async () => {
    // The endpoint 404s for a model that isn't saved, and an unchanged override is nothing to
    // say — so editing only the budget must not send a capability write at all.
    render(
      <HostedModelSettingsSheet
        model={HOSTED}
        override={{ vision: "on", context_length: 256000 }}
        onClose={() => {}}
      />,
      { wrapper },
    );

    const ctx = (await screen.findByLabelText("Hosted context window tokens")) as HTMLInputElement;
    fireEvent.change(ctx, { target: { value: "50000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockSetModelSettings).toHaveBeenCalled());
    expect(mockSetSavedModelOverride).not.toHaveBeenCalled();
  });
});
