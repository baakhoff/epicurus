import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocalModels } from "@/screens/ModelsScreen";

// ── mock downloads store (the inline settings form's "Pull variant" uses it) ──────

const mockPull = vi.fn();
vi.mock("@/stores/downloads", () => ({
  useDownloads: (selector: (s: unknown) => unknown) =>
    selector({ active: {}, pull: mockPull, dismiss: vi.fn() }),
}));

// ── mock the API client ───────────────────────────────────────────────────────

const mockModels = vi.fn();
const mockLlmPrefs = vi.fn();
const mockSystemInfo = vi.fn();
const mockModelSettings = vi.fn();
const mockModelDetails = vi.fn();
const mockSetGlobalDefault = vi.fn();
const mockSetModelHidden = vi.fn();
const mockDeleteModel = vi.fn();
const mockSetModelSettings = vi.fn();
const mockModelVariants = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    models: (caps: boolean) => mockModels(caps),
    llmPrefs: () => mockLlmPrefs(),
    systemInfo: () => mockSystemInfo(),
    modelSettings: (m: string) => mockModelSettings(m),
    modelDetails: (m: string) => mockModelDetails(m),
    modelVariants: (m: string) => mockModelVariants(m),
    setGlobalDefault: (m: string | null) => mockSetGlobalDefault(m),
    setModelHidden: (m: string, h: boolean) => mockSetModelHidden(m, h),
    deleteModel: (m: string) => mockDeleteModel(m),
    setModelSettings: (m: string, s: unknown) => mockSetModelSettings(m, s),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const MODELS = [
  { name: "llama3.2:latest", size: 4_700_000_000, loaded: true, hidden: false, capabilities: ["tools"] },
  { name: "nomic-embed-text:latest", size: 270_000_000, loaded: false, hidden: false, capabilities: [] },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockModels.mockResolvedValue(MODELS);
  // The first model is the global default; the global window is 8000 so panels inherit it.
  mockLlmPrefs.mockResolvedValue({ global_default: "llama3.2:latest", global_context_window: 8000, hidden: [] });
  mockSystemInfo.mockResolvedValue({ suggested_context: { min: 2048, suggested: 16384, max: 24000 } });
  mockModelSettings.mockResolvedValue({ context_window: null, keep_alive: null, device: null });
  mockModelDetails.mockResolvedValue({
    quantization: "Q4_K_M",
    parameter_size: "8.0B",
    context_length: 131072,
    family: "llama",
  });
  mockSetGlobalDefault.mockResolvedValue({ status: "ok" });
  mockSetModelHidden.mockResolvedValue({ status: "ok", hidden: [] });
  mockDeleteModel.mockResolvedValue({ status: "ok" });
  mockSetModelSettings.mockResolvedValue({ status: "ok" });
  mockModelVariants.mockResolvedValue({ model: "x", variants: [] });
});

describe("LocalModels", () => {
  it("lists each model collapsed — no settings panel or action buttons until tapped", async () => {
    render(<LocalModels />, { wrapper });

    expect(await screen.findByText("llama3.2:latest")).toBeInTheDocument();
    expect(screen.getByText("nomic-embed-text:latest")).toBeInTheDocument();
    // Status badge shows in the collapsed row; settings/actions are hidden until expand.
    expect(screen.getByText("loaded")).toBeInTheDocument();
    expect(screen.queryByText("Context window")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /set as default/i })).not.toBeInTheDocument();
  });

  it("expands a row on tap to reveal its inline settings and actions", async () => {
    render(<LocalModels />, { wrapper });

    fireEvent.click(
      await screen.findByRole("button", { name: "Show settings for nomic-embed-text:latest" }),
    );

    // The per-model form (context / keep-alive / run-on) and the touch-friendly action buttons.
    expect(await screen.findByText("Context window")).toBeInTheDocument();
    expect(screen.getByText("Keep-alive")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /set as default/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /hide from pickers/i })).toBeInTheDocument();
  });

  it("reads out the context window the model inherits from the global default", async () => {
    render(<LocalModels />, { wrapper });

    fireEvent.click(
      await screen.findByRole("button", { name: "Show settings for nomic-embed-text:latest" }),
    );

    expect(await screen.findByText(/tokens from the global default/i)).toHaveTextContent(
      /Inherits 8,000 tokens from the global default/i,
    );
  });

  it("keeps only one panel open at a time (accordion)", async () => {
    render(<LocalModels />, { wrapper });

    fireEvent.click(
      await screen.findByRole("button", { name: "Show settings for llama3.2:latest" }),
    );
    await screen.findByText("Context window");
    // Open the second; the first must collapse, so exactly one panel remains.
    fireEvent.click(screen.getByRole("button", { name: "Show settings for nomic-embed-text:latest" }));
    await waitFor(() => expect(screen.getAllByText("Context window")).toHaveLength(1));
  });

  it("sets a model as the global default from the panel", async () => {
    render(<LocalModels />, { wrapper });

    fireEvent.click(
      await screen.findByRole("button", { name: "Show settings for nomic-embed-text:latest" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /set as default/i }));

    await waitFor(() =>
      expect(mockSetGlobalDefault).toHaveBeenCalledWith("nomic-embed-text:latest"),
    );
  });

  it("lists registry quant variants in the panel and pulls one on tap (#330)", async () => {
    mockModelVariants.mockResolvedValue({
      model: "llama3.2:latest",
      variants: [
        { tag: "llama3.2:3b-instruct-q4_K_M", quant: "q4_K_M" },
        { tag: "llama3.2:3b-instruct-q8_0", quant: "q8_0" },
      ],
    });
    render(<LocalModels />, { wrapper });

    fireEvent.click(
      await screen.findByRole("button", { name: "Show settings for llama3.2:latest" }),
    );
    // Variants render with their quant labels; the smallest is listed first.
    expect(await screen.findByText("q8_0")).toBeInTheDocument();
    const pullButtons = screen.getAllByRole("button", { name: /^pull$/i });
    fireEvent.click(pullButtons[0]);
    // Pulling reuses the download flow with the chosen tag (q4 sorts first).
    expect(mockPull).toHaveBeenCalledWith("llama3.2:3b-instruct-q4_K_M", expect.any(Function));
  });

  it("deletes a model after the confirm dialog", async () => {
    render(<LocalModels />, { wrapper });

    fireEvent.click(
      await screen.findByRole("button", { name: "Show settings for nomic-embed-text:latest" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    // The panel's Delete only stages a confirm; nothing is deleted until the dialog is confirmed.
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText(/from disk/i)).toBeInTheDocument();
    expect(mockDeleteModel).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDeleteModel).toHaveBeenCalledWith("nomic-embed-text:latest"));
  });
});
