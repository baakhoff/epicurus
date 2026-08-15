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
const mockGetCollections = vi.fn();
const mockSaveCollections = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modules: (opts?: { refresh?: boolean }) => mockModules(opts),
    moduleConfig: (name: string) => mockModuleConfig(name),
    removeModule: (name: string) => mockRemoveModule(name),
    dockerStatus: () => mockDockerStatus(),
    getModuleCollections: (name: string) => mockGetCollections(name),
    saveModuleCollections: (name: string, prefs: unknown) => mockSaveCollections(name, prefs),
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
  mockGetCollections.mockReset();
  mockSaveCollections.mockReset();
  mockSaveCollections.mockResolvedValue({ status: "ok" });
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

/* ── one-step provider removal per module (#764, ADR-0030/ADR-0122) ─────────── */
// Going Google-free in one module must be one action against the existing prefs API —
// not N unticks, and never a token change: other modules keep their connection.

const TASKS = ModuleSnapshot.parse({
  manifest: {
    name: "tasks",
    version: "0.1.0",
    collections: { noun: "list", multi: true, providers: ["google"] },
  },
  status: { healthy: true, version: "0.1.0" },
  enabled: true,
  disabled_tools: [],
});

/** The module's `/accounts` view, merged with the stored selection (ADR-0030). */
function googleAccount(opts: { enabled: boolean; activeCollection?: string }) {
  return {
    noun: "list",
    multi: true,
    accounts: [
      {
        account: "google",
        provider: "google",
        label: "Google",
        connected: true,
        collections: [
          {
            account: "google",
            collection: "work",
            title: "Work",
            writable: true,
            enabled: opts.enabled,
            active: opts.activeCollection === "work",
          },
          {
            account: "google",
            collection: "home",
            title: "Home",
            writable: true,
            enabled: opts.enabled,
            active: opts.activeCollection === "home",
          },
        ],
      },
    ],
  };
}

async function openTasksCard() {
  mockModules.mockResolvedValue([TASKS]);
  render(<ModulesScreen />, { wrapper });
  fireEvent.click(await screen.findByRole("button", { name: /expand/i }));
}

describe("ModulesScreen per-module provider removal (#764)", () => {
  it("disables every one of the provider's collections in one write", async () => {
    mockGetCollections.mockResolvedValue(googleAccount({ enabled: true, activeCollection: "work" }));
    await openTasksCard();

    fireEvent.click(await screen.findByRole("button", { name: /Stop using Google in this module/ }));

    // One PUT, not one per collection — and the active falls back to the local default
    // because the collection it pointed at belonged to the account being dropped.
    await waitFor(() => expect(mockSaveCollections).toHaveBeenCalledTimes(1));
    expect(mockSaveCollections).toHaveBeenCalledWith("tasks", { enabled: [], active: null });
  });

  it("collapses the provider block to one quiet row once nothing is enabled", async () => {
    mockGetCollections.mockResolvedValue(googleAccount({ enabled: false }));
    await openTasksCard();

    expect(await screen.findByText("Google — not used")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use again" })).toBeInTheDocument();
    // The full-weight block is gone: no per-collection switches, no "connected" badge.
    expect(screen.queryByLabelText("Toggle Work")).not.toBeInTheDocument();
    expect(screen.queryByText("connected")).not.toBeInTheDocument();
    // …and the removal action isn't offered twice.
    expect(
      screen.queryByRole("button", { name: /Stop using Google in this module/ }),
    ).not.toBeInTheDocument();
  });

  it("says the module is local-only, rather than merely 'nothing active'", async () => {
    mockGetCollections.mockResolvedValue(googleAccount({ enabled: false }));
    await openTasksCard();
    expect(
      await screen.findByText(/Not using any connected account/),
    ).toBeInTheDocument();
  });

  it("restores the account in one click, seeded exactly as a fresh connect would", async () => {
    mockGetCollections.mockResolvedValue(googleAccount({ enabled: false }));
    await openTasksCard();

    fireEvent.click(await screen.findByRole("button", { name: "Use again" }));

    await waitFor(() => expect(mockSaveCollections).toHaveBeenCalledTimes(1));
    expect(mockSaveCollections).toHaveBeenCalledWith("tasks", {
      enabled: [
        { account: "google", collection: "work" },
        { account: "google", collection: "home" },
      ],
      // The first writable collection becomes the write target — the same seeding the core
      // performs on connect, so "use again" and "connect" leave the module in one state.
      active: { account: "google", collection: "work" },
    });
  });

  it("round-trips: stop, collapse, use again, expanded with the toggles back on", async () => {
    // The reversibility the issue asks for, end to end — the panel reflects each write after
    // the mutation's refetch, so the operator sees the state they just chose.
    mockGetCollections
      .mockResolvedValueOnce(googleAccount({ enabled: true, activeCollection: "work" }))
      .mockResolvedValue(googleAccount({ enabled: false }));
    await openTasksCard();

    fireEvent.click(await screen.findByRole("button", { name: /Stop using Google/ }));
    expect(await screen.findByText("Google — not used")).toBeInTheDocument();

    mockGetCollections.mockResolvedValue(googleAccount({ enabled: true, activeCollection: "work" }));
    fireEvent.click(screen.getByRole("button", { name: "Use again" }));

    expect(await screen.findByLabelText("Toggle Work")).toBeInTheDocument();
    expect(screen.queryByText("Google — not used")).not.toBeInTheDocument();
  });

  it("keeps an ordinary block for a connected account that simply has no collections", async () => {
    // Nothing to stop using — "not used" would be a non-sequitur, and the operator still
    // needs to see that the account is connected.
    mockGetCollections.mockResolvedValue({
      noun: "list",
      multi: true,
      accounts: [
        { account: "google", provider: "google", label: "Google", connected: true, collections: [] },
      ],
    });
    await openTasksCard();

    expect(await screen.findByText(/No lists found in this account/)).toBeInTheDocument();
    expect(screen.queryByText("Google — not used")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Stop using Google in this module/ }),
    ).not.toBeInTheDocument();
  });

  it("never offers removal for an account that isn't connected", async () => {
    mockGetCollections.mockResolvedValue({
      noun: "list",
      multi: true,
      accounts: [
        { account: "google", provider: "google", label: "Google", connected: false, collections: [] },
      ],
    });
    await openTasksCard();

    expect(await screen.findByRole("button", { name: "Connect" })).toBeInTheDocument();
    expect(screen.queryByText("Google — not used")).not.toBeInTheDocument();
  });
});
