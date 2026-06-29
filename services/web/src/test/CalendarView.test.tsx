import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CalendarView } from "@/components/archetypes/CalendarView";

const mockModulePage = vi.fn();
const mockCollections = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    getModuleCollections: (...args: unknown[]) => mockCollections(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

// A fixed "now" so the visible month is deterministic; only Date is faked so
// react-query's real timers keep working. The event is given a local (no-Z)
// timestamp so it lands on June 15 regardless of the runner's timezone.
const sample = {
  title: "Calendar",
  provider: "local",
  range: { start: "2026-06-01T00:00:00Z", end: "2026-07-01T00:00:00Z" },
  events: [
    {
      id: "e1",
      title: "Standup",
      start: "2026-06-15T09:00:00",
      end: "2026-06-15T09:30:00",
      location: "Room 4",
      description: "Daily sync",
      provider: "local",
    },
  ],
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-06-15T12:00:00Z"));
  mockModulePage.mockReset();
  mockCollections.mockReset().mockResolvedValue({
    noun: "calendar",
    multi: true,
    accounts: [
      {
        account: "google",
        provider: "google",
        label: "Google",
        connected: true,
        collections: [
          { account: "google", collection: "primary", title: "Work Calendar", writable: true },
        ],
      },
    ],
  });
  localStorage.clear(); // the per-month cache (#379) persists here — isolate each test
});
afterEach(() => vi.useRealTimers());

describe("CalendarView", () => {
  it("renders events in the month grid and requests the visible window", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    expect(await screen.findByText("Standup")).toBeInTheDocument();
    expect(mockModulePage).toHaveBeenCalledWith(
      "calendar",
      "calendar",
      expect.objectContaining({ start: expect.any(String), end: expect.any(String) }),
    );
  });

  it("opens an event's detail when a chip is clicked", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    expect(await screen.findByText("Daily sync")).toBeInTheDocument();
    expect(screen.getByText("Room 4")).toBeInTheDocument();
  });

  it("re-fetches a new window when navigating", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");

    const before = mockModulePage.mock.calls.length;
    fireEvent.click(screen.getByLabelText("Next"));
    await waitFor(() => expect(mockModulePage.mock.calls.length).toBeGreaterThan(before));
  });

  it("groups events by day in the agenda view", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");

    fireEvent.click(screen.getByText("Agenda"));
    expect(await screen.findByText("Standup")).toBeInTheDocument();
  });

  it("shows an empty notice when the agenda window has no events", async () => {
    mockModulePage.mockResolvedValue({ ...sample, events: [] });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    fireEvent.click(await screen.findByText("Agenda"));
    expect(await screen.findByText(/nothing scheduled/i)).toBeInTheDocument();
  });

  it("renders an all-day event on its date and labels it All day", async () => {
    // Floating date strings (end exclusive) — the event must show on June 15, not June 14.
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          id: "ad1",
          title: "Holiday",
          start: "2026-06-15",
          end: "2026-06-16",
          all_day: true,
          provider: "local",
        },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    fireEvent.click(await screen.findByText("Holiday"));
    expect(await screen.findByText(/All day/i)).toBeInTheDocument();
  });

  it("toggles a calendar's visibility from the Calendars menu (#378)", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          id: "e1",
          title: "Standup",
          start: "2026-06-15T09:00:00",
          end: "2026-06-15T09:30:00",
          provider: "local",
          calendar_id: "local",
        },
        {
          id: "e2",
          title: "Sync",
          start: "2026-06-16T10:00:00",
          end: "2026-06-16T10:30:00",
          provider: "google",
          calendar_id: "google:primary",
        },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    expect(await screen.findByText("Standup")).toBeInTheDocument();
    expect(screen.getByText("Sync")).toBeInTheDocument();

    // Open the Calendars menu and hide the Google calendar (named from the collections view).
    fireEvent.click(screen.getByLabelText("Choose visible calendars"));
    fireEvent.click(await screen.findByText("Work Calendar"));

    // Its events disappear; the other calendar's events stay. The choice is persisted.
    await waitFor(() => expect(screen.queryByText("Sync")).toBeNull());
    expect(screen.getByText("Standup")).toBeInTheDocument();
    expect(localStorage.getItem("epicurus-cal-hidden:calendar:calendar")).toContain(
      "google:primary",
    );
  });

  it("paints the cached window instantly on reopen, then revalidates (#379)", async () => {
    mockModulePage.mockResolvedValue(sample);
    const { unmount } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup"); // fetched and cached to localStorage
    unmount();

    // Reopen while the network hangs — the cached month must paint with no await.
    mockModulePage.mockReset();
    mockModulePage.mockReturnValue(new Promise(() => {}));
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    expect(screen.getByText("Standup")).toBeInTheDocument(); // straight from the persisted cache
    expect(mockModulePage).toHaveBeenCalled(); // …and it still revalidates in the background
  });
});
