import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { TimezoneCard } from "@/screens/SettingsScreen";

const mockTimezone = vi.fn();
const mockSetTimezone = vi.fn();
const mockModuleStatus = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    timezone: (...a: unknown[]) => mockTimezone(...a),
    setTimezone: (...a: unknown[]) => mockSetTimezone(...a),
    moduleStatus: (...a: unknown[]) => mockModuleStatus(...a),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("TimezoneCard", () => {
  it("renders the stored timezone and saves a change on blur", async () => {
    mockTimezone.mockResolvedValue({ timezone: "Europe/Belgrade" });
    mockModuleStatus.mockResolvedValue({ google_timezone: "Europe/Belgrade" });
    mockSetTimezone.mockResolvedValue({ status: "ok", timezone: "Asia/Tokyo" });

    render(<TimezoneCard />, { wrapper });

    const input = await screen.findByDisplayValue("Europe/Belgrade");
    fireEvent.change(input, { target: { value: "Asia/Tokyo" } });
    fireEvent.blur(input);
    await waitFor(() => expect(mockSetTimezone).toHaveBeenCalledWith("Asia/Tokyo"));
  });

  it("flags a calendar timezone that differs from the setting", async () => {
    mockTimezone.mockResolvedValue({ timezone: "UTC" });
    mockModuleStatus.mockResolvedValue({ google_timezone: "Europe/Belgrade" });

    render(<TimezoneCard />, { wrapper });

    // The mismatch hint + a one-click "use it" button appear.
    expect(await screen.findByRole("button", { name: /Use Europe\/Belgrade/ })).toBeInTheDocument();
  });
});
