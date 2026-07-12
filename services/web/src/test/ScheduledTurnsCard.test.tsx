import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ScheduledTurnsCard } from "@/components/ScheduledTurnsCard";
import type { ScheduledTurn } from "@/lib/contracts";

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockSetEnabled = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(detail);
      this.status = status;
      this.detail = detail;
    }
  },
  api: {
    scheduledTurns: (...a: unknown[]) => mockList(...a),
    createScheduledTurn: (...a: unknown[]) => mockCreate(...a),
    setScheduledTurnEnabled: (...a: unknown[]) => mockSetEnabled(...a),
    deleteScheduledTurn: (...a: unknown[]) => mockDelete(...a),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function turn(overrides: Partial<ScheduledTurn> = {}): ScheduledTurn {
  return {
    id: "st1",
    prompt: "Summarize my day",
    cadence: "daily",
    hour: 7,
    weekday: null,
    delivery_target: "scheduled-abc",
    enabled: true,
    created_at: "2026-07-01T00:00:00Z",
    last_run_at: null,
    last_status: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockList.mockReset();
  mockCreate.mockReset().mockResolvedValue(turn());
  mockSetEnabled.mockReset().mockResolvedValue({});
  mockDelete.mockReset().mockResolvedValue(undefined);
});

describe("ScheduledTurnsCard (#526, ADR-0092)", () => {
  it("shows an empty state when nothing is scheduled", async () => {
    mockList.mockResolvedValue([]);
    render(<ScheduledTurnsCard />, { wrapper });
    expect(await screen.findByText(/nothing scheduled yet/i)).toBeInTheDocument();
  });

  it("lists a scheduled turn with its cadence and last-run summary", async () => {
    mockList.mockResolvedValue([
      turn({ last_run_at: "2026-07-12T07:00:00Z", last_status: "ok" }),
    ]);
    render(<ScheduledTurnsCard />, { wrapper });

    expect(await screen.findByText("Summarize my day")).toBeInTheDocument();
    expect(screen.getByText(/daily at 07:00/i)).toBeInTheDocument();
    expect(screen.getByText(/last ran/i)).toBeInTheDocument();
  });

  it("shows a weekly turn's weekday in its cadence label", async () => {
    mockList.mockResolvedValue([turn({ cadence: "weekly", hour: 9, weekday: 2 })]);
    render(<ScheduledTurnsCard />, { wrapper });
    expect(await screen.findByText(/weekly on wednesday at 09:00/i)).toBeInTheDocument();
  });

  it("shows the paused-skip reason distinctly from a real run", async () => {
    mockList.mockResolvedValue([
      turn({ last_run_at: "2026-07-12T07:00:00Z", last_status: "skipped (paused)" }),
    ]);
    render(<ScheduledTurnsCard />, { wrapper });
    expect(await screen.findByText(/skipped.*runtime was paused/i)).toBeInTheDocument();
  });

  it("creates a daily scheduled turn from the New form", async () => {
    mockList.mockResolvedValue([]);
    render(<ScheduledTurnsCard />, { wrapper });
    await screen.findByText(/nothing scheduled yet/i);

    fireEvent.click(screen.getByRole("button", { name: /new/i }));
    fireEvent.change(screen.getByLabelText(/prompt/i), {
      target: { value: "Check my calendar" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith({
        prompt: "Check my calendar",
        cadence: "daily",
        hour: 7,
        weekday: null,
      }),
    );
  });

  it("a weekly cadence reveals the weekday picker and includes it on save", async () => {
    mockList.mockResolvedValue([]);
    render(<ScheduledTurnsCard />, { wrapper });
    await screen.findByText(/nothing scheduled yet/i);

    fireEvent.click(screen.getByRole("button", { name: /new/i }));
    fireEvent.change(screen.getByLabelText(/prompt/i), { target: { value: "Weekly review" } });
    fireEvent.change(screen.getByLabelText(/cadence/i), { target: { value: "weekly" } });
    expect(screen.getByLabelText(/^on$/i)).toBeInTheDocument(); // the weekday picker appeared
    fireEvent.change(screen.getByLabelText(/^on$/i), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith({
        prompt: "Weekly review",
        cadence: "weekly",
        hour: 7,
        weekday: 2,
      }),
    );
  });

  it("toggling the switch calls setScheduledTurnEnabled", async () => {
    mockList.mockResolvedValue([turn()]);
    render(<ScheduledTurnsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("switch"));
    await waitFor(() => expect(mockSetEnabled).toHaveBeenCalledWith("st1", false));
  });

  it("deleting a turn calls deleteScheduledTurn", async () => {
    mockList.mockResolvedValue([turn()]);
    render(<ScheduledTurnsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /delete scheduled turn/i }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("st1"));
  });

  it("shows a load error without crashing", async () => {
    mockList.mockRejectedValue(new Error("unreachable"));
    render(<ScheduledTurnsCard />, { wrapper });
    expect(await screen.findByText(/could not load/i)).toBeInTheDocument();
  });
});
