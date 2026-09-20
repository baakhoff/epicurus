/**
 * A module's model slots on a deployment with no local runtime (#962).
 *
 * The slot select is built from the local model list plus, for a chat slot, the saved hosted
 * ones. With no runtime the local half is permanently empty, so an embedding slot collapses to
 * a lone "Core default" — a section that reads as broken unless it says why it is empty.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModuleSnapshot } from "@/lib/contracts";
import { ModulesScreen } from "@/screens/ModulesScreen";

const mockModules = vi.fn();
const mockModels = vi.fn();
const mockLocalRuntime = vi.fn();
const mockSavedModels = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modules: () => mockModules(),
    moduleConfig: vi.fn().mockResolvedValue({}),
    dockerStatus: vi.fn().mockResolvedValue({ available: true, reason: null }),
    getModuleCollections: vi.fn().mockResolvedValue({}),
    saveModuleCollections: vi.fn().mockResolvedValue({ status: "ok" }),
    getModuleModels: vi.fn().mockResolvedValue({}),
    setModuleModels: vi.fn().mockResolvedValue({ status: "ok" }),
    models: () => mockModels(),
    savedModels: () => mockSavedModels(),
    localRuntime: () => mockLocalRuntime(),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const INDEXER = ModuleSnapshot.parse({
  manifest: {
    name: "knowledge",
    version: "0.1.0",
    required_models: [{ key: "embed", label: "Embedding model", role: "embedding" }],
  },
  status: { healthy: true, version: "0.1.0" },
  enabled: true,
  disabled_tools: [],
});

async function expandCard() {
  fireEvent.click(await screen.findByRole("button", { name: /expand/i }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mockModules.mockResolvedValue([INDEXER]);
  mockModels.mockResolvedValue([]);
  mockSavedModels.mockResolvedValue([]);
  mockLocalRuntime.mockResolvedValue({ state: "ok", url_configured: true });
});

describe("Module model slots without a local runtime", () => {
  it("explains an otherwise-empty slot instead of leaving it blank", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "absent", url_configured: false });
    render(<ModulesScreen />, { wrapper });
    await expandCard();

    expect(await screen.findByText(/No local AI on this deployment/i)).toBeInTheDocument();
  });

  it("says nothing extra when the deployment has a runtime", async () => {
    render(<ModulesScreen />, { wrapper });
    await expandCard();

    expect(await screen.findByText("Embedding model")).toBeInTheDocument();
    expect(screen.queryByText(/No local AI on this deployment/i)).toBeNull();
  });
});
