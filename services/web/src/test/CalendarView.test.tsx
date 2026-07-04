import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CalendarView } from "@/components/archetypes/CalendarView";

const mockModulePage = vi.fn();
const mockCollections = vi.fn();
const mockModules = vi.fn();
const mockInvoke = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    getModuleCollections: (...args: unknown[]) => mockCollections(...args),
    modules: (...args: unknown[]) => mockModules(...args),
    invokeModuleTool: (...args: unknown[]) => mockInvoke(...args),
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
  mockModules.mockReset().mockResolvedValue([]);
  mockInvoke.mockReset();
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

  it("shows a recurring event's repeat rule and guest list in its detail (#432)", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          id: "e1",
          title: "Standup",
          start: "2026-06-15T09:00:00",
          end: "2026-06-15T09:30:00",
          recurrence: "FREQ=WEEKLY;COUNT=4",
          attendees: [
            { email: "alice@example.com" },
            { email: "bob@example.com", display_name: "Bob" },
          ],
        },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    expect(await screen.findByText("Weekly")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com, Bob")).toBeInTheDocument();
  });

  it("omits the repeat/guest lines for a plain event", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    await screen.findByText("Daily sync");
    expect(screen.queryByText(/^(Weekly|Daily|Monthly|Yearly)$/)).toBeNull();
  });

  it("shows a Join with Google Meet link when the event has one (#444)", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          id: "e1",
          title: "Standup",
          start: "2026-06-15T09:00:00",
          end: "2026-06-15T09:30:00",
          meet_url: "https://meet.google.com/abc-defg-hij",
        },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    const link = await screen.findByRole("link", { name: "Join with Google Meet" });
    expect(link).toHaveAttribute("href", "https://meet.google.com/abc-defg-hij");
  });

  it("omits the Meet link for an event without one", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    await screen.findByText("Daily sync");
    expect(screen.queryByRole("link", { name: "Join with Google Meet" })).toBeNull();
  });

  it("drops a Meet link with a non-http(s) scheme", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          id: "e1",
          title: "Standup",
          start: "2026-06-15T09:00:00",
          end: "2026-06-15T09:30:00",
          description: "Daily sync",
          meet_url: "javascript:alert(1)",
        },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    await screen.findByText("Daily sync");
    expect(screen.queryByRole("link", { name: "Join with Google Meet" })).toBeNull();
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

  // Regression guard (#427): the page-level action ("New event") must match the
  // toolbar's other hand-rolled controls (Today, view switcher: text-xs), not the
  // full form-sized Button used e.g. by the tasks board toolbar.
  it("renders the page-level action at the toolbar's compact size", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      actions: [{ tool: "calendar_create_event", label: "New event" }],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    const button = await screen.findByRole("button", { name: /new event/i });
    expect(button.className).toContain("text-xs");
    expect(button.className).not.toContain("text-sm");
  });

  it("lists every enabled calendar in the menu, not only those with in-window events (#431)", async () => {
    mockCollections.mockResolvedValue({
      noun: "calendar",
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
              collection: "primary",
              title: "Work Calendar",
              writable: true,
              enabled: true,
            },
            {
              account: "google",
              collection: "family@group",
              title: "Family",
              writable: true,
              enabled: true,
            },
            {
              account: "google",
              collection: "off@group",
              title: "Disabled one",
              writable: true,
              enabled: false,
            },
          ],
        },
      ],
    });
    // Only one calendar has an event in this window — the menu must still list both
    // enabled calendars (and not the disabled one).
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
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
    await screen.findByText("Sync");

    fireEvent.click(await screen.findByLabelText("Choose visible calendars"));
    expect(await screen.findByText("Work Calendar")).toBeInTheDocument();
    expect(screen.getByText("Family")).toBeInTheDocument(); // enabled but empty this window
    expect(screen.queryByText("Disabled one")).toBeNull(); // not enabled → no toggle
  });

  it("tints event chips with the calendar's own colour, matching the menu dot (#431)", async () => {
    mockCollections.mockResolvedValue({
      noun: "calendar",
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
              collection: "primary",
              title: "Work Calendar",
              writable: true,
              enabled: true,
              color: "#af4fd7",
            },
            {
              account: "google",
              collection: "family@group",
              title: "Family",
              writable: true,
              enabled: true,
            },
          ],
        },
      ],
    });
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
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

    // The chip carries the provider's colour as its --cal variable.
    const chip = await screen.findByText("Sync");
    expect(chip.closest("button")?.style.getPropertyValue("--cal")).toBe("#af4fd7");
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

// The modal focus contract (#512): EventDetail is a hand-rolled role="dialog" outside
// ui.tsx's kit — it must honor the same keyboard contract Sheet/Confirm got in #487:
// focus moves in on open, Tab wraps inside, and the opener chip regains focus on close.
describe("EventDetail focus management (#512)", () => {
  it("moves focus into the dialog on open and returns it to the chip on close", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    const chip = (await screen.findByText("Standup")).closest("button");
    if (!chip) throw new Error("event chip not rendered");
    chip.focus();
    fireEvent.click(chip);

    const dialog = await screen.findByRole("dialog", { name: "Standup" });
    expect(dialog).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(chip).toHaveFocus();
  });

  it("wraps Tab at the dialog edges instead of walking the calendar underneath", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          id: "e1",
          title: "Standup",
          start: "2026-06-15T09:00:00",
          end: "2026-06-15T09:30:00",
          meet_url: "https://meet.google.com/abc-defg-hij",
        },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    fireEvent.click(await screen.findByText("Standup"));
    await screen.findByRole("dialog", { name: "Standup" });

    // Focusables in DOM order: the Close button first, then the Meet link.
    const close = screen.getByRole("button", { name: "Close" });
    const link = screen.getByRole("link", { name: "Join with Google Meet" });

    link.focus();
    fireEvent.keyDown(link, { key: "Tab" });
    expect(close).toHaveFocus(); // forward from the last wraps to the first

    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(link).toHaveFocus(); // backward from the first wraps to the last
  });
});
