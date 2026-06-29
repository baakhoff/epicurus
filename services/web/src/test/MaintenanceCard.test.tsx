import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MaintenanceCard } from "@/screens/SettingsScreen";

const mockStatus = vi.fn();
const mockRun = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    maintenanceStatus: () => mockStatus(),
    runMaintenance: () => mockRun(),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

type Status = {
  schedule_enabled: boolean;
  schedule_hour: number;
  jobs: { key: string; label: string; nightly: boolean }[];
  last_run: unknown;
};

const STATUS = (over: Partial<Status> = {}): Status => ({
  schedule_enabled: false,
  schedule_hour: 4,
  jobs: [
    { key: "memory-extraction", label: "Memory fact extraction", nightly: true },
    { key: "module-reindex", label: "Module re-index / re-embed", nightly: false },
  ],
  last_run: null,
  ...over,
});

beforeEach(() => {
  mockStatus.mockReset();
  mockRun.mockReset();
});

describe("MaintenanceCard", () => {
  it("shows the manual-only schedule state", async () => {
    mockStatus.mockResolvedValue(STATUS());
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/manual only/i)).toBeInTheDocument();
  });

  it("shows the nightly schedule when enabled", async () => {
    mockStatus.mockResolvedValue(STATUS({ schedule_enabled: true, schedule_hour: 4 }));
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/scheduled nightly at 04:00/i)).toBeInTheDocument();
  });

  it("runs the batch and renders the per-job result (#383)", async () => {
    mockStatus.mockResolvedValue(STATUS());
    mockRun.mockResolvedValue({
      ran_at: "2026-06-29T04:00:00+00:00",
      scope: "all",
      jobs: [
        {
          key: "memory-extraction",
          label: "Memory fact extraction",
          status: "ok",
          detail: "distilled 3 pending exchange(s)",
        },
        {
          key: "module-reindex",
          label: "Module re-index / re-embed",
          status: "skipped",
          detail: "no reindexable modules",
        },
      ],
    });
    render(<MaintenanceCard />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: /run maintenance now/i }));
    await waitFor(() => expect(mockRun).toHaveBeenCalled());
    expect(await screen.findByText(/distilled 3 pending exchange/i)).toBeInTheDocument();
    expect(screen.getByText(/no reindexable modules/i)).toBeInTheDocument();
  });
});
