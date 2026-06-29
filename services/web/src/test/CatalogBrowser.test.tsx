import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CatalogBrowser } from "@/screens/ModelsScreen";
import { CATALOG, filterCatalog, type CatalogTag } from "@/data/catalog";
import { api } from "@/lib/api";
import type { CatalogResponse } from "@/lib/contracts";

// ── mock downloads store ──────────────────────────────────────────────────────

const mockPull = vi.fn();
const mockDismiss = vi.fn();
let mockActive: Record<string, { model: string; status: string; done: boolean }> = {};

vi.mock("@/stores/downloads", () => ({
  useDownloads: (selector: (s: unknown) => unknown) =>
    selector({ active: mockActive, pull: mockPull, dismiss: mockDismiss }),
}));

// ── mock the catalog fetch — CatalogBrowser now reads it from the core (#269) ──

vi.mock("@/lib/api", () => ({ api: { catalog: vi.fn(), systemInfo: vi.fn() } }));

const mockCatalog = vi.mocked(api.catalog);
const mockSystemInfo = vi.mocked(api.systemInfo);

function snapshot(over: Partial<CatalogResponse> = {}): CatalogResponse {
  return {
    entries: CATALOG,
    source: "https://ollama.com/library",
    updated_at: new Date(),
    stale: false,
    ...over,
  };
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

// ── filterCatalog (pure function) ─────────────────────────────────────────────

describe("filterCatalog", () => {
  const tags = (...t: CatalogTag[]) => new Set<CatalogTag>(t);
  const NONE = tags();

  it("returns all entries with no query and no tags", () => {
    expect(filterCatalog(CATALOG, "", NONE)).toHaveLength(CATALOG.length);
  });

  it("filters by model id (case-insensitive)", () => {
    const results = filterCatalog(CATALOG, "LLAMA3.2", NONE);
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.id.toLowerCase()).toContain("llama3.2");
    });
  });

  it("filters by family name", () => {
    const results = filterCatalog(CATALOG, "gemma", NONE);
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.family.toLowerCase()).toContain("gemma");
    });
  });

  it("filters by description word", () => {
    const results = filterCatalog(CATALOG, "RAG", NONE);
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.description.toLowerCase()).toContain("rag");
    });
  });

  it("filters by a single tag", () => {
    const results = filterCatalog(CATALOG, "", tags("embedding"));
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.tags).toContain("embedding");
    });
  });

  it("ANDs across multiple checked tags — an entry must carry all of them (#389)", () => {
    const both = filterCatalog(CATALOG, "", tags("vision", "multilingual"));
    expect(both.length).toBeGreaterThan(0);
    both.forEach((e) => {
      expect(e.tags).toContain("vision");
      expect(e.tags).toContain("multilingual");
    });
    // The AND result is never larger than either single-tag result.
    expect(both.length).toBeLessThanOrEqual(filterCatalog(CATALOG, "", tags("vision")).length);
  });

  it("returns empty when the combined tags exclude every entry", () => {
    // No catalog model is both an embedding model and a code model.
    expect(filterCatalog(CATALOG, "", tags("embedding", "code"))).toHaveLength(0);
  });

  it("combines query and tags (AND logic)", () => {
    const codeResults = filterCatalog(CATALOG, "qwen", tags("code"));
    expect(codeResults.length).toBeGreaterThan(0);
    codeResults.forEach((e) => {
      expect(e.tags).toContain("code");
      expect(
        e.id.toLowerCase().includes("qwen") ||
          e.family.toLowerCase().includes("qwen") ||
          e.description.toLowerCase().includes("qwen"),
      ).toBe(true);
    });
  });

  it("returns empty list when no match", () => {
    expect(filterCatalog(CATALOG, "zzznomatch", NONE)).toHaveLength(0);
  });

  it("returns empty list when a tag excludes all matches", () => {
    expect(filterCatalog(CATALOG, "llama", tags("embedding"))).toHaveLength(0);
  });
});

// ── CatalogBrowser component ──────────────────────────────────────────────────

beforeEach(() => {
  mockActive = {};
  mockPull.mockClear();
  mockCatalog.mockReset();
  mockCatalog.mockResolvedValue(snapshot());
  mockSystemInfo.mockReset();
  // A modest 8 GB GPU / 16 GB RAM box gives the catalog a clear mix of fit ratings — small
  // models "Fit", the 70B is "Too big" — so the fit-filter chips have something to bite on (#388).
  mockSystemInfo.mockResolvedValue({
    gpu: { vendor: "nvidia", name: "Test GPU", vram_total_mb: 8 * 1024 },
    ram_total_mb: 16 * 1024,
  });
});

describe("CatalogBrowser", () => {
  it("renders the search input and tag filter chips", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    expect(screen.getByRole("textbox", { name: /search catalog/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Code" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Embedding" })).toBeInTheDocument();
    // Capability filters now include Vision and Tools, so the operator can find models that
    // can call tools / see images (#model-caps).
    expect(screen.getByRole("button", { name: "Vision" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tools" })).toBeInTheDocument();
  });

  it("shows the upstream source once the catalog loads", async () => {
    mockCatalog.mockResolvedValue(snapshot({ source: "https://ollama.com/library" }));
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    expect(await screen.findByText(/from ollama\.com\/library/i)).toBeInTheDocument();
  });

  it("falls back to the bundled list and says so when the catalog is unreachable", async () => {
    mockCatalog.mockRejectedValue(new Error("404"));
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    // The bundled seed still renders (never empty)…
    expect(screen.getByText("llama3.2:3b")).toBeInTheDocument();
    // …and the provenance line explains the fallback.
    expect(await screen.findByText(/showing the built-in list/i)).toBeInTheDocument();
  });

  it("renders a live entry's params and pull count from the snapshot", async () => {
    mockCatalog.mockResolvedValue(
      snapshot({
        entries: [
          {
            id: "qwen3:8b",
            family: "qwen3",
            params: "8b",
            size_gb: null,
            description: "Live entry parsed upstream.",
            tags: ["general"],
            pulls: "31.3M",
          },
        ],
      }),
    );
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    expect(await screen.findByText("qwen3:8b")).toBeInTheDocument();
    expect(screen.getByText(/31\.3M pulls/)).toBeInTheDocument();
  });

  it("renders Pull buttons for non-installed models", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    const pulls = screen.getAllByRole("button", { name: /^pull$/i });
    expect(pulls.length).toBeGreaterThan(0);
  });

  it("shows Installed badge instead of Pull for already-installed models", () => {
    render(<CatalogBrowser installed={new Set(["llama3.2:3b"])} />, { wrapper });
    const installed = screen.getAllByText("Installed");
    expect(installed.length).toBeGreaterThanOrEqual(1);
  });

  it("calls pull() when Pull button is clicked", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    const firstPull = screen.getAllByRole("button", { name: /^pull$/i })[0];
    fireEvent.click(firstPull);
    expect(mockPull).toHaveBeenCalledOnce();
  });

  it("filters entries when the user types in the search box", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    const input = screen.getByRole("textbox", { name: /search catalog/i });
    fireEvent.change(input, { target: { value: "llava" } });
    expect(screen.getByText("llava:7b")).toBeInTheDocument();
    expect(screen.queryByText("gemma3:1b")).not.toBeInTheDocument();
  });

  it("filters entries when a tag chip is clicked", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /^vision$/i }));
    const ids = screen.getAllByText(/moondream|llava|llama3\.2-vision/);
    expect(ids.length).toBeGreaterThan(0);
    expect(screen.queryByText("gemma3:1b")).not.toBeInTheDocument();
  });

  it("shows empty-state message when search has no results", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    const input = screen.getByRole("textbox", { name: /search catalog/i });
    fireEvent.change(input, { target: { value: "zzznomatch" } });
    expect(screen.getByText(/no models match/i)).toBeInTheDocument();
  });

  it("shows Pulling… and disables button for in-progress downloads", () => {
    mockActive = {
      "gemma3:1b": { model: "gemma3:1b", status: "downloading", done: false },
    };
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    expect(screen.getByText(/pulling…/i)).toBeInTheDocument();
  });

  it("clicking the same tag chip twice deactivates the filter", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    const chip = screen.getByRole("button", { name: /^code$/i });
    fireEvent.click(chip);
    expect(screen.queryByText("gemma3:1b")).not.toBeInTheDocument();
    fireEvent.click(chip);
    expect(screen.getByText("gemma3:1b")).toBeInTheDocument();
  });

  it("ANDs two tag chips — narrows to models carrying both (#389)", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /^vision$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^multilingual$/i }));
    // gemma4 carries both vision + multilingual; a vision-only model (llava) drops out.
    expect(screen.getByText("gemma4:e4b")).toBeInTheDocument();
    expect(screen.queryByText("llava:7b")).not.toBeInTheDocument();
  });

  it("the All chip clears every selected tag", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /^code$/i }));
    expect(screen.queryByText("gemma3:1b")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    expect(screen.getByText("gemma3:1b")).toBeInTheDocument();
  });

  it("shows fit-rating filter chips once the system is known (#388)", async () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    expect(await screen.findByRole("button", { name: /too big/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^fits$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^tight$/i })).toBeInTheDocument();
  });

  it("filters the catalog by fit rating (#388)", async () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    // "Too big" keeps the 70B (43 GB) model on the 8 GB GPU and drops a tiny one that fits.
    fireEvent.click(await screen.findByRole("button", { name: /too big/i }));
    expect(screen.getByText("llama3.3:70b")).toBeInTheDocument();
    expect(screen.queryByText("smollm2:135m")).not.toBeInTheDocument();
  });
});
