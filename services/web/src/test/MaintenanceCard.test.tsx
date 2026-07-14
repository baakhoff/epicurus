import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { MaintenanceCard } from "@/screens/SettingsScreen";

const mockStatus = vi.fn();
const mockRun = vi.fn();
const mockSetSchedule = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ApiError: actual.ApiError,
    api: {
      maintenanceStatus: () => mockStatus(),
      runMaintenance: () => mockRun(),
      setMaintenanceSchedule: (update: unknown) => mockSetSchedule(update),
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
  schedule_cadence: string;
  schedule_hour: number;
  schedule_weekday: number | null;
  next_run_at: string | null;
  jobs: { key: string; label: string; nightly: boolean }[];
  last_run: unknown;
  current_run: CurrentRun | null;
};

const STATUS = (over: Partial<Status> = {}): Status => ({
  schedule_enabled: false,
  schedule_cadence: "daily",
  schedule_hour: 4,
  schedule_weekday: null,
  next_run_at: null,
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
  mockSetSchedule.mockReset();
});

describe("MaintenanceCard", () => {
  it("shows the manual-only schedule state", async () => {
    mockStatus.mockResolvedValue(STATUS());
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/manual only/i)).toBeInTheDocument();
  });

  it("shows the daily schedule summary when enabled", async () => {
    mockStatus.mockResolvedValue(
      STATUS({ schedule_enabled: true, schedule_cadence: "daily", schedule_hour: 4 }),
    );
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/runs daily at 04:00/i)).toBeInTheDocument();
  });

  it("shows the hourly schedule summary", async () => {
    mockStatus.mockResolvedValue(STATUS({ schedule_enabled: true, schedule_cadence: "hourly" }));
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/runs every hour/i)).toBeInTheDocument();
  });

  it("shows the weekly schedule summary with the weekday name", async () => {
    mockStatus.mockResolvedValue(
      STATUS({
        schedule_enabled: true,
        schedule_cadence: "weekly",
        schedule_hour: 3,
        schedule_weekday: 0,
      }),
    );
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/runs weekly on monday at 03:00/i)).toBeInTheDocument();
  });

  it("shows the estimated next run time when enabled", async () => {
    mockStatus.mockResolvedValue(
      STATUS({ schedule_enabled: true, next_run_at: "2026-07-14T04:00:00+00:00" }),
    );
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByText(/next run/i)).toBeInTheDocument();
  });

  it("hides the next-run estimate when the schedule is disabled", async () => {
    mockStatus.mockResolvedValue(STATUS({ schedule_enabled: false, next_run_at: null }));
    render(<MaintenanceCard />, { wrapper });
    await screen.findByText(/manual only/i);
    expect(screen.queryByText(/next run/i)).not.toBeInTheDocument();
  });

  it("the Save button starts disabled until the draft actually changes", async () => {
    mockStatus.mockResolvedValue(STATUS());
    render(<MaintenanceCard />, { wrapper });
    expect(await screen.findByRole("button", { name: /save schedule/i })).toBeDisabled();

    fireEvent.click(screen.getByRole("switch", { name: /enable scheduled maintenance/i }));
    expect(screen.getByRole("button", { name: /save schedule/i })).toBeEnabled();
  });

  it("saves the enabled toggle and cadence/hour as a whole", async () => {
    mockStatus.mockResolvedValue(STATUS());
    mockSetSchedule.mockResolvedValue(STATUS({ schedule_enabled: true }));
    render(<MaintenanceCard />, { wrapper });

    fireEvent.click(await screen.findByRole("switch", { name: /enable scheduled maintenance/i }));
    fireEvent.click(screen.getByRole("button", { name: /save schedule/i }));

    await waitFor(() =>
      expect(mockSetSchedule).toHaveBeenCalledWith({
        enabled: true,
        cadence: "daily",
        hour: 4,
        weekday: null,
      }),
    );
  });

  it("switching to weekly reveals a weekday picker and saves it", async () => {
    mockStatus.mockResolvedValue(STATUS({ schedule_enabled: true }));
    mockSetSchedule.mockResolvedValue(STATUS({ schedule_enabled: true, schedule_cadence: "weekly" }));
    render(<MaintenanceCard />, { wrapper });

    fireEvent.change(await screen.findByLabelText(/cadence/i), { target: { value: "weekly" } });
    expect(screen.getByLabelText(/^on$/i)).toBeInTheDocument(); // the weekday select appeared
    fireEvent.change(screen.getByLabelText(/^on$/i), { target: { value: "2" } }); // Wednesday
    fireEvent.click(screen.getByRole("button", { name: /save schedule/i }));

    await waitFor(() =>
      expect(mockSetSchedule).toHaveBeenCalledWith({
        enabled: true,
        cadence: "weekly",
        hour: 4,
        weekday: 2,
      }),
    );
  });

  it("hides the hour and weekday pickers for an hourly cadence", async () => {
    mockStatus.mockResolvedValue(STATUS({ schedule_enabled: true }));
    render(<MaintenanceCard />, { wrapper });
    fireEvent.change(await screen.findByLabelText(/cadence/i), { target: { value: "hourly" } });
    expect(screen.queryByLabelText(/^at$/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^on$/i)).not.toBeInTheDocument();
  });

  it("shows an error and does not update the summary when the save is rejected", async () => {
    mockStatus.mockResolvedValue(STATUS());
    mockSetSchedule.mockRejectedValue(new ApiError(400, "hour must be 0-23"));
    render(<MaintenanceCard />, { wrapper });

    fireEvent.click(await screen.findByRole("switch", { name: /enable scheduled maintenance/i }));
    fireEvent.click(screen.getByRole("button", { name: /save schedule/i }));

    expect(await screen.findByText(/could not save the schedule/i)).toBeInTheDocument();
    expect(screen.getByText(/manual only/i)).toBeInTheDocument(); // unchanged — the PUT failed
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
