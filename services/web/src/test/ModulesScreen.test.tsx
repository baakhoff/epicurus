import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModuleSnapshot } from "@/lib/contracts";
import { ModulesScreen } from "@/screens/ModulesScreen";

const mockModules = vi.fn();
const mockModuleConfig = vi.fn();
const mockRemoveModule = vi.fn();
const mockDockerStatus = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modules: (opts?: { refresh?: boolean }) => mockModules(opts),
    moduleConfig: (name: string) => mockModuleConfig(name),
    removeModule: (name: string) => mockRemoveModule(name),
    dockerStatus: () => mockDockerStatus(),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

// A minimal healthy module with no UI section, so the card's config/models/collections
// sub-sections short-circuit and the test only exercises the removal flow. Parsed through the
// contract so every defaulted field (tags, tools, …) is present, exactly as `api.modules` returns.
const ECHO = ModuleSnapshot.parse({
  manifest: { name: "echo", version: "0.1.0" },
  status: { healthy: true, version: "0.1.0" },
  enabled: true,
  disabled_tools: [],
});

async function openDangerZoneAndRemove() {
  // Expand the card, then open the confirm dialog and confirm.
  const expand = await screen.findByRole("button", { name: /expand/i });
  fireEvent.click(expand);
  fireEvent.click(await screen.findByRole("button", { name: /remove module/i }));
  // The confirm dialog has its own "Remove module" button; click the last match (the dialog's).
  const confirmButtons = await screen.findAllByRole("button", { name: /remove module/i });
  fireEvent.click(confirmButtons[confirmButtons.length - 1]);
}

beforeEach(() => {
  mockModules.mockReset();
  mockModuleConfig.mockReset();
  mockRemoveModule.mockReset();
  mockDockerStatus.mockReset();
  mockModules.mockResolvedValue([ECHO]);
  mockModuleConfig.mockResolvedValue({});
  mockDockerStatus.mockResolvedValue({ available: true, reason: null });
});

describe("ModulesScreen removal", () => {
  it("shows an informational deferred-teardown notice when the core has no Docker access (#382)", async () => {
    mockRemoveModule.mockResolvedValue({
      removed: "echo",
      containers: 0,
      container_teardown_deferred: true,
    });
    render(<ModulesScreen />, { wrapper });
    await openDangerZoneAndRemove();

    await waitFor(() => expect(mockRemoveModule).toHaveBeenCalledWith("echo"));
    // The notice names the module and explains the container keeps running until restart.
    const notice = await screen.findByText(/its container is still running/i);
    expect(notice.textContent).toMatch(/echo/);
    expect(notice.textContent).toMatch(/next restart/i);
    // It is informational, not the red error path.
    expect(screen.queryByText(/module discovery is down/i)).toBeNull();
  });

  it("shows no deferred notice on a normal removal (container torn down now)", async () => {
    mockRemoveModule.mockResolvedValue({
      removed: "echo",
      containers: 1,
      container_teardown_deferred: false,
    });
    render(<ModulesScreen />, { wrapper });
    await openDangerZoneAndRemove();

    await waitFor(() => expect(mockRemoveModule).toHaveBeenCalledWith("echo"));
    // No deferred-teardown banner — the container is already gone.
    await waitFor(() =>
      expect(screen.queryByText(/its container is still running/i)).toBeNull(),
    );
  });
});

describe("ModulesScreen Docker status (#622)", () => {
  it("shows no status card when Docker is reachable", async () => {
    render(<ModulesScreen />, { wrapper });
    await screen.findByText("echo");
    expect(screen.queryByText(/isn.t reachable from the core/i)).toBeNull();
  });

  it("shows an accurate, proactive card — never 'removal disabled' — when Docker is unreachable", async () => {
    mockDockerStatus.mockResolvedValue({
      available: false,
      reason: "permission denied while trying to connect",
    });
    render(<ModulesScreen />, { wrapper });

    // Query the plain-text portion (a sibling of the emphasized span) so the match bubbles
    // up to the whole paragraph, which also carries the interpolated reason.
    const paragraph = await screen.findByText(/module removal still works immediately/i);
    expect(paragraph.textContent).toMatch(/isn.t reachable from the core/i);
    expect(paragraph.textContent).toMatch(/permission denied while trying to connect/);
    // Removal itself is never described as disabled (ADR-0056/#382 decoupled the two) —
    // only container teardown / the KV-cache restart defer.
    expect(screen.queryByText(/removal disabled/i)).toBeNull();
    expect(await screen.findByText(/DOCKER_GID/)).toBeTruthy();
  });

  it("omits the parenthetical reason when the probe captured none", async () => {
    mockDockerStatus.mockResolvedValue({ available: false, reason: null });
    render(<ModulesScreen />, { wrapper });

    const paragraph = await screen.findByText(/module removal still works immediately/i);
    expect(paragraph.textContent).not.toMatch(/\(\)/);
  });
});

describe("ModulesScreen refresh (#478)", () => {
  it("bypasses the probe cache when the operator clicks refresh", async () => {
    render(<ModulesScreen />, { wrapper });
    await screen.findByText("echo");
    mockModules.mockClear(); // drop the initial (cache-served) load call

    fireEvent.click(screen.getByRole("button", { name: /refresh module health/i }));

    await waitFor(() => expect(mockModules).toHaveBeenCalledWith({ refresh: true }));
  });
});
