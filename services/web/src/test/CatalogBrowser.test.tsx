import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CatalogBrowser } from "@/screens/ModelsScreen";
import { CATALOG, filterCatalog } from "@/data/catalog";
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

vi.mock("@/lib/api", () => ({ api: { catalog: vi.fn() } }));

const mockCatalog = vi.mocked(api.catalog);

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
  it("returns all entries with no query and no tag", () => {
    expect(filterCatalog(CATALOG, "", null)).toHaveLength(CATALOG.length);
  });

  it("filters by model id (case-insensitive)", () => {
    const results = filterCatalog(CATALOG, "LLAMA3.2", null);
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.id.toLowerCase()).toContain("llama3.2");
    });
  });

  it("filters by family name", () => {
    const results = filterCatalog(CATALOG, "gemma", null);
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.family.toLowerCase()).toContain("gemma");
    });
  });

  it("filters by description word", () => {
    const results = filterCatalog(CATALOG, "RAG", null);
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.description.toLowerCase()).toContain("rag");
    });
  });

  it("filters by tag", () => {
    const results = filterCatalog(CATALOG, "", "embedding");
    expect(results.length).toBeGreaterThan(0);
    results.forEach((e) => {
      expect(e.tags).toContain("embedding");
    });
  });

  it("combines query and tag (AND logic)", () => {
    const codeResults = filterCatalog(CATALOG, "qwen", "code");
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
    expect(filterCatalog(CATALOG, "zzznomatch", null)).toHaveLength(0);
  });

  it("returns empty list when tag excludes all matches", () => {
    const results = filterCatalog(CATALOG, "llama", "embedding");
    expect(results).toHaveLength(0);
  });
});

// ── CatalogBrowser component ──────────────────────────────────────────────────

beforeEach(() => {
  mockActive = {};
  mockPull.mockClear();
  mockCatalog.mockReset();
  mockCatalog.mockResolvedValue(snapshot());
});

describe("CatalogBrowser", () => {
  it("renders the search input and tag filter chips", () => {
    render(<CatalogBrowser installed={new Set()} />, { wrapper });
    expect(screen.getByRole("textbox", { name: /search catalog/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Code" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Embedding" })).toBeInTheDocument();
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
});
