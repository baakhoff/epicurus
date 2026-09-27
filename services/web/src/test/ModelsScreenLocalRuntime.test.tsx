/**
 * The Models page against the three local-runtime states (#962).
 *
 * `absent` is a deployment that runs no local runtime at all (`OLLAMA_URL=""`), which is a
 * supported mode and not a fault: the local half of the page collapses into one line rather
 * than showing five cards of Ollama controls that can never do anything. `unreachable` keeps
 * today's warning, because that state *is* an error. `ok` must not regress — it is the common
 * case.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModelsScreen } from "@/screens/ModelsScreen";

vi.mock("@/stores/downloads", () => ({
  useDownloads: (selector: (s: unknown) => unknown) =>
    selector({ active: {}, pull: vi.fn(), dismiss: vi.fn() }),
}));

const mockModels = vi.fn();
const mockLocalRuntime = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    models: (caps?: boolean) => mockModels(caps),
    localRuntime: () => mockLocalRuntime(),
    catalog: vi.fn().mockResolvedValue({ source: "https://example.test", entries: [] }),
    systemInfo: vi.fn().mockResolvedValue({
      suggested_context: { min: 2048, suggested: 8192, max: 16384 },
    }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    }),
    savedModels: vi.fn().mockResolvedValue([]),
    providers: vi.fn().mockResolvedValue([]),
    recallDimension: vi.fn().mockResolvedValue({ status: "ok", detail: "" }),
    modelSettings: vi.fn().mockResolvedValue({ context_window: null, keep_alive: null, device: null }),
    modelDetails: vi.fn().mockResolvedValue({ capabilities: [] }),
    modelVariants: vi.fn().mockResolvedValue({ model: "x", variants: [] }),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const LOCAL_MODEL = {
  name: "llama3.2:latest",
  size: 4_700_000_000,
  loaded: false,
  hidden: false,
  capabilities: [],
};

/** The optgroup that must not be offerable when no runtime can serve it. */
const localOptgroup = () => document.querySelector('optgroup[label="Local (Ollama)"]');

beforeEach(() => {
  vi.clearAllMocks();
  mockModels.mockResolvedValue([LOCAL_MODEL]);
  mockLocalRuntime.mockResolvedValue({ state: "ok", url_configured: true });
});

describe("Models page — no local runtime (absent)", () => {
  beforeEach(() => {
    mockLocalRuntime.mockResolvedValue({ state: "absent", url_configured: false });
    // The contract: the model list is a plain, successful, empty 200 — not an error.
    mockModels.mockResolvedValue([]);
  });

  it("collapses the local half into one honest line", async () => {
    render(<ModelsScreen />, { wrapper });

    expect(await screen.findByTestId("local-runtime-absent")).toHaveTextContent(
      /Local AI is not configured on this deployment/i,
    );
    // And says the part that matters: nothing is broken, hosted keeps working.
    expect(screen.getByTestId("local-runtime-absent")).toHaveTextContent(
      /Hosted models are unaffected/i,
    );
  });

  it("removes the pull card, the catalog, the local list, the context window and the KV cache", async () => {
    render(<ModelsScreen />, { wrapper });
    await screen.findByTestId("local-runtime-absent");

    // Removed from the render, not disabled — a disabled control still reads as a fault.
    expect(screen.queryByText("Browse models")).toBeNull();
    expect(screen.queryByText("Local models")).toBeNull();
    expect(screen.queryByText("Default context window")).toBeNull();
    expect(screen.queryByRole("heading", { name: "KV-cache type" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^pull$/i })).toBeNull();
  });

  it("never calls the absent state an error", async () => {
    render(<ModelsScreen />, { wrapper });
    await screen.findByTestId("local-runtime-absent");

    expect(screen.queryByText(/is the ollama service up/i)).toBeNull();
    expect(screen.queryByText(/unreachable/i)).toBeNull();
  });

  it("keeps the hosted half of the page", async () => {
    render(<ModelsScreen />, { wrapper });
    await screen.findByTestId("local-runtime-absent");

    expect(screen.getByText("Embedding model")).toBeInTheDocument();
    expect(screen.getByText("Hosted providers")).toBeInTheDocument();
    expect(screen.getByText("Hosted models")).toBeInTheDocument();
  });

  it("drops the Local (Ollama) optgroup from the embedding picker", async () => {
    render(<ModelsScreen />, { wrapper });
    await screen.findByTestId("local-runtime-absent");

    await waitFor(() => expect(screen.getByText("System default")).toBeInTheDocument());
    expect(localOptgroup()).toBeNull();
  });
});

describe("Models page — the runtime is unreachable", () => {
  beforeEach(() => {
    mockLocalRuntime.mockResolvedValue({ state: "unreachable", url_configured: true });
    // The core answers 200 + [] now, so nothing but the state endpoint knows it is down.
    mockModels.mockResolvedValue([]);
  });

  it("keeps the warning — that state is an error and still looks like one", async () => {
    render(<ModelsScreen />, { wrapper });

    expect(await screen.findByText(/is the ollama service up/i)).toBeInTheDocument();
  });

  it("does not claim local AI is unconfigured, and keeps the local cards", async () => {
    render(<ModelsScreen />, { wrapper });
    await screen.findByText(/is the ollama service up/i);

    expect(screen.queryByTestId("local-runtime-absent")).toBeNull();
    expect(screen.getByText("Local models")).toBeInTheDocument();
    expect(screen.getByText("Browse models")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "KV-cache type" })).toBeInTheDocument();
  });

  it("does not also invite a pull into a runtime that is down", async () => {
    render(<ModelsScreen />, { wrapper });
    await screen.findByText(/is the ollama service up/i);

    // An empty 200 used to be impossible here; now it must not read as "no models yet".
    expect(screen.queryByText(/None yet\. Pull one above/i)).toBeNull();
  });
});

describe("Models page — a healthy runtime (ok)", () => {
  it("renders the full local UI", async () => {
    render(<ModelsScreen />, { wrapper });

    expect(await screen.findByText("Local models")).toBeInTheDocument();
    expect(screen.getByText("Browse models")).toBeInTheDocument();
    expect(screen.getByText("Default context window")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "KV-cache type" })).toBeInTheDocument();
    expect(screen.queryByTestId("local-runtime-absent")).toBeNull();
    expect(screen.queryByText(/is the ollama service up/i)).toBeNull();
  });

  it("offers the Local (Ollama) optgroup in the embedding picker", async () => {
    render(<ModelsScreen />, { wrapper });
    await screen.findByText("Embedding model");

    await waitFor(() => expect(localOptgroup()).not.toBeNull());
    expect(localOptgroup()).toHaveTextContent("llama3.2:latest");
  });
});

/**
 * The regression guard for the thing that will silently rot: the state is read from
 * `/llm/local-runtime`, never inferred from `/llm/models`. The two used to be the same signal —
 * an unreachable runtime made the model list 500 — and since #962 it does not, so any code that
 * drifts back to inferring stops working with no test failing.
 */
describe("Models page — runtime state comes from its own endpoint", () => {
  it("collapses on `absent` even though the model list answered successfully", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "absent", url_configured: false });
    mockModels.mockResolvedValue([LOCAL_MODEL]); // a 200 with content, not an error

    render(<ModelsScreen />, { wrapper });

    expect(await screen.findByTestId("local-runtime-absent")).toBeInTheDocument();
    expect(screen.queryByText("Local models")).toBeNull();
  });

  it("warns on `unreachable` even though the model list answered 200 with []", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "unreachable", url_configured: true });
    mockModels.mockResolvedValue([]);

    render(<ModelsScreen />, { wrapper });

    // Nothing failed. Only the state endpoint knows, and the warning still appears.
    expect(await screen.findByText(/is the ollama service up/i)).toBeInTheDocument();
  });

  it("does not collapse when the model list fails but the runtime reports ok", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "ok", url_configured: true });
    mockModels.mockRejectedValue(new Error("boom"));

    render(<ModelsScreen />, { wrapper });

    expect(await screen.findByText("Local models")).toBeInTheDocument();
    expect(screen.queryByTestId("local-runtime-absent")).toBeNull();
  });

  it("keeps today's behaviour when the endpoint is missing (an older core)", async () => {
    mockLocalRuntime.mockRejectedValue(new Error("404 Not Found"));

    render(<ModelsScreen />, { wrapper });

    // An unknown answer is `ok`: hiding the local half from a deployment that has a runtime
    // would be a far worse failure than showing it to one that doesn't.
    expect(await screen.findByText("Local models")).toBeInTheDocument();
    expect(screen.queryByTestId("local-runtime-absent")).toBeNull();
  });
});
