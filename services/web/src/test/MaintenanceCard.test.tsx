import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { MaintenanceCard } from "@/screens/SettingsScreen";

const mockStatus = vi.fn();
const mockRun = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ApiError: actual.ApiError,
    api: {
      maintenanceStatus: () => mockStatus(),
      runMaintenance: () => mockRun(),
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

type JobProgress = { key: string; label: string; status: string; detail: string };
type CurrentRun = { started_at: string; scope: string; jobs: JobProgress[] };
type Status = {
  schedule_enabled: boolean;
  schedule_hour: number;
  jobs: { key: string; label: string; nightly: boolean }[];
  last_run: unknown;
  current_run: CurrentRun | null;
};

const STATUS = (over: Partial<Status> = {}): Status => ({
  schedule_enabled: false,
  schedule_hour: 4,
  jobs: [
    { key: "memory-extraction", label: "Memory fact extraction", nightly: true },
    { key: "module-reindex", label: "Module re-index / re-embed", nightly: false },
  ],
  last_run: null,
  current_run: null,
  ...over,
});

const CURRENT_RUN = (over: Partial<CurrentRun> = {}): CurrentRun => ({
  started_at: "2026-07-09T04:00:00+00:00",
  scope: "all",
  jobs: [
    { key: "memory-extraction", label: "Memory fact extraction", status: "running", detail: "" },
    { key: "module-reindex", label: "Module re-index / re-embed", status: "pending", detail: "" },
  ],
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

  it("renders the last run's per-job result when idle (#383)", async () => {
    mockStatus.mockResolvedValue(
      STATUS({
        last_run: {
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
        },
      }),
    );
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/distilled 3 pending exchange/i)).toBeInTheDocument();
    expect(screen.getByText(/no reindexable modules/i)).toBeInTheDocument();
  });

  it("rehydrates onto an in-flight run on mount and shows live per-job progress (#561)", async () => {
    mockStatus.mockResolvedValue(STATUS({ current_run: CURRENT_RUN() }));
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/memory fact extraction — running/i)).toBeInTheDocument();
    expect(screen.getByText(/0\/2 jobs/i)).toBeInTheDocument();
    // Can't start a second run while one is already live — and the button says so.
    expect(screen.getByRole("button", { name: /running/i })).toBeDisabled();
  });

  it("starts a run and shows progress once the request resolves (#561)", async () => {
    mockStatus
      .mockResolvedValueOnce(STATUS())
      .mockResolvedValue(STATUS({ current_run: CURRENT_RUN() }));
    mockRun.mockResolvedValue(CURRENT_RUN());
    render(<MaintenanceCard />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: /run maintenance now/i }));
    await waitFor(() => expect(mockRun).toHaveBeenCalled());
    expect(await screen.findByText(/memory fact extraction — running/i)).toBeInTheDocument();
  });

  it("treats a 409 conflict as joining the in-flight run, not an error (#561)", async () => {
    mockStatus
      .mockResolvedValueOnce(STATUS())
      .mockResolvedValue(STATUS({ current_run: CURRENT_RUN() }));
    mockRun.mockRejectedValue(new ApiError(409, "a maintenance run is already in progress"));
    render(<MaintenanceCard />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: /run maintenance now/i }));
    await waitFor(() => expect(mockRun).toHaveBeenCalled());
    // No red error banner — the card just joins the run that's already going.
    expect(await screen.findByText(/memory fact extraction — running/i)).toBeInTheDocument();
    expect(screen.queryByText(/already in progress/i)).not.toBeInTheDocument();
  });

  it("shows an error banner for a non-conflict failure", async () => {
    mockStatus.mockResolvedValue(STATUS());
    mockRun.mockRejectedValue(new ApiError(500, "boom"));
    render(<MaintenanceCard />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: /run maintenance now/i }));
    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
  });
});
