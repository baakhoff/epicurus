import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModuleSnapshot } from "@/lib/contracts";
import { SettingsScreen } from "@/screens/SettingsScreen";

// Stubbed so this file tests SettingsScreen's own gating logic, not these cards'
// internals (each has its own test file).
vi.mock("@/components/ChatBridgesCard", () => ({
  ChatBridgesCard: () => <div>STUB chat bridges card</div>,
}));
vi.mock("@/components/MemorySection", () => ({
  MemorySection: () => null,
}));

const mockModules = vi.fn();
const mockInfo = vi.fn();
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ApiError: actual.ApiError,
    api: {
      info: () => mockInfo(),
      modules: () => mockModules(),
      eventSubscriptions: () => Promise.resolve([]),
      setEventSubscription: vi.fn(),
      oauthClientStatus: () => Promise.resolve({ configured: true }),
      oauthStatus: () => Promise.resolve({ connected: false, scope: null }),
      oauthConnect: vi.fn(),
      oauthDisconnect: vi.fn(),
      oauthSetClient: vi.fn(),
      timezone: () => Promise.resolve({ timezone: "UTC" }),
      moduleStatus: () => Promise.resolve({}),
      setTimezone: vi.fn(),
      llmPrefs: () => Promise.resolve({ global_agent_max_steps: null }),
      setAgentMaxSteps: vi.fn(),
      agentInstructions: () =>
        Promise.resolve({ instructions: "Default prompt.", is_default: true }),
      setAgentInstructions: vi.fn(),
      maintenanceStatus: () =>
        Promise.resolve({
          schedule_enabled: false,
          schedule_hour: 3,
          jobs: [],
          last_run: null,
          current_run: null,
        }),
      runMaintenance: vi.fn(),
      maintenanceRunHistory: () => Promise.resolve({ runs: [], next_cursor: null }),
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const MESSAGING_ENABLED = ModuleSnapshot.parse({
  manifest: { name: "messaging", version: "0.3.0" },
  status: { healthy: true, version: "0.3.0" },
  enabled: true,
});
const MESSAGING_DISABLED = ModuleSnapshot.parse({
  manifest: { name: "messaging", version: "0.3.0" },
  status: { healthy: true, version: "0.3.0" },
  enabled: false,
});

const INFO = {
  contract_version: "0.1",
  core_version: "0.37.0",
  core_app_version: "0.122.0",
  library_version: "0.37.0",
  release_track: "testing",
  tenant: "local",
};

beforeEach(() => {
  mockModules.mockReset();
  mockModules.mockResolvedValue([]);
  mockInfo.mockReset();
  mockInfo.mockResolvedValue(INFO);
});

// #430: Chat bridges are a messaging-module capability, so the card (and its API
// calls) must only mount when messaging is installed *and* enabled.
describe("SettingsScreen — Chat bridges gating", () => {
  it("hides the card when the messaging module is absent", async () => {
    mockModules.mockResolvedValue([]);
    render(<SettingsScreen />, { wrapper });

    await waitFor(() => expect(mockModules).toHaveBeenCalled());
    expect(screen.queryByText(/stub chat bridges card/i)).not.toBeInTheDocument();
  });

  it("hides the card when messaging is installed but disabled", async () => {
    mockModules.mockResolvedValue([MESSAGING_DISABLED]);
    render(<SettingsScreen />, { wrapper });

    await waitFor(() => expect(mockModules).toHaveBeenCalled());
    expect(screen.queryByText(/stub chat bridges card/i)).not.toBeInTheDocument();
  });

  it("shows the card when messaging is installed and enabled", async () => {
    mockModules.mockResolvedValue([MESSAGING_ENABLED]);
    render(<SettingsScreen />, { wrapper });

    expect(await screen.findByText(/stub chat bridges card/i)).toBeInTheDocument();
  });
});

// #893: the card labelled `epicurus_core`'s version "core version", so the one number an
// operator quotes in a bug report named the wrong component. The two are now shown apart,
// beside the image tag that was actually pulled.
describe("SettingsScreen — the Platform card", () => {
  it("names the service, the library and the track as three separate facts", async () => {
    render(<SettingsScreen />, { wrapper });

    const coreApp = await screen.findByText("core-app");
    expect(coreApp.nextElementSibling).toHaveTextContent("0.122.0");
    expect(screen.getByText("library").nextElementSibling).toHaveTextContent("0.37.0");
    expect(screen.getByText("track").nextElementSibling).toHaveTextContent("testing");
    expect(screen.getByText("contract").nextElementSibling).toHaveTextContent("0.1");
    expect(screen.getByText("tenant").nextElementSibling).toHaveTextContent("local");
    // The old label is gone: it was the mislabel, not merely a duplicate.
    expect(screen.queryByText("core version")).not.toBeInTheDocument();
  });

  it("draws an em dash for a deployment that pulled no tagged image", async () => {
    // A source checkout: the core answers `null` rather than guessing "latest", and the
    // card must not dress that up as a track either.
    mockInfo.mockResolvedValue({ ...INFO, release_track: null });
    render(<SettingsScreen />, { wrapper });

    expect((await screen.findByText("track")).nextElementSibling).toHaveTextContent("—");
  });
});
