import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUR_HEIGHT } from "@/components/archetypes/calendarGrid";
import { CalendarView } from "@/components/archetypes/CalendarView";
import { usePanel } from "@/stores/panel";

const mockModulePage = vi.fn();
const mockCollections = vi.fn();
const mockModules = vi.fn();
const mockInvoke = vi.fn();
const mockCalendarFeed = vi.fn();
const mockResolveEntity = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    getModuleCollections: (...args: unknown[]) => mockCollections(...args),
    modules: (...args: unknown[]) => mockModules(...args),
    invokeModuleTool: (...args: unknown[]) => mockInvoke(...args),
    calendarFeed: (...args: unknown[]) => mockCalendarFeed(...args),
    resolveEntity: (...args: unknown[]) => mockResolveEntity(...args),
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

// A minimal `calendar_create_event` tool schema (#473) — the default empty `mockModules`
// fixture (below) makes `ActionControl`'s form schema empty, which is fine for tests that
// never open the sheet; slot-create tests need real `start`/`end`/`all_day` properties to
// assert the pre-fill actually reaches the rendered fields.
const CALENDAR_MODULE_WITH_CREATE_SCHEMA = [
  {
    manifest: {
      name: "calendar",
      version: "1.0.0",
      description: "",
      contract_version: "0.1",
      tags: [],
      tools: [
        {
          name: "calendar_create_event",
          description: "",
          input_schema: {
            type: "object",
            properties: {
              title: { type: "string", title: "Title" },
              start: { type: "string", format: "date-time", date_toggle: "all_day", title: "Start" },
              end: { type: "string", format: "date-time", date_toggle: "all_day", title: "End" },
              all_day: { type: "boolean", title: "All day" },
              // Present so `form_values.calendar_id` (the existing default, #473 must not
              // clobber it) actually round-trips into the submitted payload — SchemaForm only
              // carries values for keys the schema itself declares.
              calendar_id: { type: "string", title: "Calendar" },
            },
            required: [],
          },
        },
      ],
      events_emitted: [],
      events_consumed: [],
      config: [],
      secrets: [],
      pages: [],
      resolver: false,
      attachable: false,
      required_models: [],
    },
    status: { healthy: true, version: "1.0.0" },
    enabled: true,
    disabled_tools: [],
  },
];

/** The first day cell in the month grid that doesn't hold the "Standup" fixture event. */
function findEmptyDayCell(container: HTMLElement): HTMLElement {
  const grid = container.querySelector(".grid-rows-6");
  if (!grid) throw new Error("month grid not rendered");
  const cell = [...grid.children].find(
    (c) => !c.textContent?.includes("Standup"),
  ) as HTMLElement | undefined;
  if (!cell) throw new Error("no empty day cell found");
  return cell;
}

/** True once the week hourly grid (#631) is rendered — keyed off its inline column template,
 *  which is locale-independent (unlike the gutter's hour labels). */
const weekGridUp = (container: HTMLElement): boolean =>
  container.querySelector('[style*="minmax(6rem"]') !== null;

/** A day column in the week grid: a `relative` cell whose direct child is the absolute event
 *  layer — distinguishing it from the (also-`relative`) time-gutter hour cells. */
function weekDayColumn(container: HTMLElement): HTMLElement {
  const col = [...container.querySelectorAll("div.relative")].find((d) =>
    d.querySelector(":scope > .absolute.inset-0"),
  ) as HTMLElement | undefined;
  if (!col) throw new Error("no week day column found");
  return col;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-06-15T12:00:00Z"));
  mockModulePage.mockReset();
  mockModules.mockReset().mockResolvedValue([]);
  mockInvoke.mockReset();
  mockCalendarFeed.mockReset().mockResolvedValue([]);
  mockResolveEntity.mockReset();
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

  it("renders a failed event action's error below the full actions row, not between the buttons (#472)", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          ...sample.events[0],
          actions: [
            { tool: "calendar_rsvp_event", label: "Accept", args: { event_id: "e1" } },
            { tool: "calendar_decline_event", label: "Decline", args: { event_id: "e1" } },
          ],
        },
      ],
    });
    mockInvoke.mockRejectedValue(new Error("NetworkError when attempting to fetch resource"));
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    const acceptBtn = await screen.findByRole("button", { name: "Accept" });
    const row = acceptBtn.closest("div")!;
    fireEvent.click(acceptBtn);

    const error = await screen.findByText("NetworkError when attempting to fetch resource");
    // The row still holds only its buttons — the error is not interposed between them.
    expect(within(row).getByRole("button", { name: "Decline" })).toBeInTheDocument();
    expect(within(row).queryByText(error.textContent!)).toBeNull();
    // It renders as the row's next sibling, i.e. below the full row.
    expect(row.nextElementSibling).toBe(error);
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

  // Narrow-viewport icon-only (#562): the toolbar action opts into ActionControl's
  // responsive shrink, which keeps the accessible name on aria-label + a tooltip
  // regardless of which of the two (CSS-driven) label spans is currently visible —
  // jsdom doesn't evaluate the `sm:` breakpoint, so this asserts the DOM contract
  // rather than the visual state (checked live in a real browser separately).
  it("keeps the New event action's accessible name and label available at every width (#562)", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      actions: [{ tool: "calendar_create_event", label: "New event", icon: "plus" }],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    const button = await screen.findByRole("button", { name: "New event" });
    expect(button).toHaveAttribute("aria-label", "New event");
    expect(screen.getByRole("tooltip")).toHaveTextContent("New event");
    // The label text itself still renders (hidden below `sm` by CSS, not removed from the DOM).
    expect(button).toHaveTextContent("New event");
  });

  it("renders the month label in both its full and narrow-viewport short form (#562)", async () => {
    mockModulePage.mockResolvedValue(sample);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    await screen.findByText("Standup");
    expect(screen.getByText("June 2026")).toBeInTheDocument(); // full — shown at/above `sm`
    expect(screen.getByText("Jun 2026")).toBeInTheDocument(); // short — shown below `sm`
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
              color: "#fbd75b",
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
        {
          id: "e3",
          title: "Picnic",
          start: "2026-06-16T12:00:00",
          end: "2026-06-16T13:00:00",
          provider: "google",
          calendar_id: "google:family@group",
        },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    // The chip carries the provider's colour as its --cal variable, and a computed
    // AA-safe hover text colour as --cal-ink (#531): this mid-tone purple sits in the
    // crossover band where neither house ink nor white clears 4.5:1 — pure black does.
    const chip = await screen.findByText("Sync");
    expect(chip.closest("button")?.style.getPropertyValue("--cal")).toBe("#af4fd7");
    expect(chip.closest("button")?.style.getPropertyValue("--cal-ink")).toBe("#000000");

    // A light calendar colour gets the house near-black, never the old text-canvas
    // (which in the light theme washed out on a light fill — the #531 failure).
    const light = await screen.findByText("Picnic");
    expect(light.closest("button")?.style.getPropertyValue("--cal-ink")).toBe("#121411");
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

// #630 moves event creation off the month day-tap (which now navigates into that day's week
// view) and onto the explicit affordances: the toolbar "New event" and the week grid's empty
// slots — the #473 slot-seed create, relocated from the month cell to the grid.
describe("Month tap-through & week slot-create (#630)", () => {
  const withCreate = {
    ...sample,
    actions: [{ tool: "calendar_create_event", label: "New event", form: true, form_values: {} }],
  };

  it("opens that day's week view on a month-cell tap, not the create form", async () => {
    mockModules.mockResolvedValue(CALENDAR_MODULE_WITH_CREATE_SCHEMA);
    mockModulePage.mockResolvedValue(withCreate);
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");

    fireEvent.click(findEmptyDayCell(container));
    await waitFor(() => expect(weekGridUp(container)).toBe(true)); // landed in the week grid
    expect(screen.queryByLabelText("Start")).not.toBeInTheDocument(); // and not a create form
  });

  it("highlights the tapped day in the week it lands on", async () => {
    mockModulePage.mockResolvedValue(sample);
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");

    const cell = findEmptyDayCell(container);
    const dayNum = cell.querySelector("span.rounded-full")!.textContent!;
    fireEvent.click(cell);
    await waitFor(() => expect(weekGridUp(container)).toBe(true));
    // The focused day's date badge carries the accent ring (distinct from today's filled pip).
    const focusedBadge = [...container.querySelectorAll("div.ring-accent")].find(
      (d) => d.textContent === dayNum,
    );
    expect(focusedBadge).toBeTruthy();
  });

  it("opens the create form with a timed start when a week empty slot is tapped", async () => {
    mockModules.mockResolvedValue(CALENDAR_MODULE_WITH_CREATE_SCHEMA);
    mockModulePage.mockResolvedValue(withCreate);
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");
    fireEvent.click(screen.getByRole("button", { name: "Week" }));
    await waitFor(() => expect(weekGridUp(container)).toBe(true));

    fireEvent.click(weekDayColumn(container), { clientY: 240 });
    const startInput = (await screen.findByLabelText("Start")) as HTMLInputElement;
    expect(startInput).toHaveAttribute("type", "datetime-local"); // timed, not all-day
    expect(screen.getByRole("switch", { name: "All day" })).toHaveAttribute("aria-checked", "false");
  });

  it("submits a week slot create as a timed event", async () => {
    mockModules.mockResolvedValue(CALENDAR_MODULE_WITH_CREATE_SCHEMA);
    mockModulePage.mockResolvedValue({
      ...sample,
      actions: [
        {
          tool: "calendar_create_event",
          label: "New event",
          form: true,
          form_values: { calendar_id: "local" },
        },
      ],
    });
    mockInvoke.mockResolvedValue({});
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");
    fireEvent.click(screen.getByRole("button", { name: "Week" }));
    await waitFor(() => expect(weekGridUp(container)).toBe(true));

    fireEvent.click(weekDayColumn(container), { clientY: 240 });
    const sheet = screen.getByRole("dialog", { name: "New event" });
    fireEvent.click(within(sheet).getByRole("button", { name: "New event" }));
    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith(
        "calendar",
        "calendar_create_event",
        expect.objectContaining({ calendar_id: "local", all_day: false }),
      ),
    );
  });

  it("does not open the create form when an event chip is clicked", async () => {
    mockModules.mockResolvedValue(CALENDAR_MODULE_WITH_CREATE_SCHEMA);
    mockModulePage.mockResolvedValue(withCreate);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    expect(await screen.findByRole("dialog", { name: "Standup" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Start")).not.toBeInTheDocument();
  });

  it('opens detail, not create, when the "+N more" button is clicked', async () => {
    mockModules.mockResolvedValue(CALENDAR_MODULE_WITH_CREATE_SCHEMA);
    const busyDay = Array.from({ length: 5 }, (_, i) => ({
      id: `e${i}`,
      title: `Event ${i}`,
      start: "2026-06-15T09:00:00",
      end: "2026-06-15T09:30:00",
    }));
    mockModulePage.mockResolvedValue({
      ...sample,
      events: busyDay,
      actions: [
        { tool: "calendar_create_event", label: "New event", form: true, form_values: {} },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("+2 more")); // desktop overflow chip
    expect(await screen.findByRole("dialog")).toBeInTheDocument(); // opened event detail…
    expect(screen.queryByLabelText("Start")).not.toBeInTheDocument(); // …not the create form
  });

  it("still navigates to the week view when the page declares no create action", async () => {
    mockModulePage.mockResolvedValue(sample); // no `actions` at all
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");

    fireEvent.click(findEmptyDayCell(container));
    await waitFor(() => expect(weekGridUp(container)).toBe(true)); // navigation isn't gated on create
    expect(screen.queryByLabelText("Start")).not.toBeInTheDocument();
  });
});

// #632: on a phone every event renders as a slim textless colour line — density over labels,
// since detail now lives one tap away in the week view.
describe("Month density on mobile (#632)", () => {
  const daySpread = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: `e${i}`,
      title: `Event ${i}`,
      start: "2026-06-15T09:00:00",
      end: "2026-06-15T09:30:00",
      calendar_id: "local",
    }));

  it("draws one slim line per event, with no premature overflow indicator", async () => {
    mockModulePage.mockResolvedValue({ ...sample, events: daySpread(6) });
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Event 0"); // desktop chip present

    // 6 events → 6 slim lines in the mobile lane; the desktop lane holds labelled chips, not these.
    expect(container.querySelectorAll("div.h-1.rounded-full")).toHaveLength(6);
    expect(screen.queryByText(/^\+\d+$/)).toBeNull(); // no "+N" overflow marker
  });

  it("collapses to a +N marker only past what genuinely fits", async () => {
    mockModulePage.mockResolvedValue({ ...sample, events: daySpread(12) });
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Event 0");

    // Capped at the fit limit (10), the remaining 2 collapse into a slim "+2".
    expect(container.querySelectorAll("div.h-1.rounded-full")).toHaveLength(10);
    expect(screen.getByText("+2")).toBeInTheDocument();
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

describe("Task-feed overlay (#469)", () => {
  const FEED_ITEM = {
    id: "t1",
    title: "Buy milk",
    date: "2026-06-15",
    status: "open",
    ref_id: "t1",
    kind: "task",
    module: "tasks",
  };

  beforeEach(() => usePanel.getState().close());

  it("renders a task-feed chip on its due day, distinct from an event chip", async () => {
    mockModulePage.mockResolvedValue(sample);
    mockCalendarFeed.mockResolvedValue([FEED_ITEM]);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    const chip = (await screen.findByText("Buy milk")).closest("button")!;
    expect(chip.querySelector(".lucide-square-check")).toBeInTheDocument();
    expect(mockCalendarFeed).toHaveBeenCalledWith("2026-06-01", "2026-07-13"); // month grid bounds
    // Still shows the real event too — the feed overlays, it never replaces.
    expect(screen.getByText("Standup")).toBeInTheDocument();
  });

  it("opens the owning module's hover-card in the panel on click, read-only", async () => {
    mockModulePage.mockResolvedValue(sample);
    mockCalendarFeed.mockResolvedValue([FEED_ITEM]);
    mockResolveEntity.mockResolvedValue({
      title: "Buy milk",
      description: "",
      details: [
        { label: "Due", value: "2026-06-15" },
        { label: "Status", value: "Open" },
      ],
      href: { label: "Open in Tasks", url: "/m/tasks/board" },
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Buy milk"));
    await waitFor(() =>
      expect(mockResolveEntity).toHaveBeenCalledWith("tasks", "task", "t1"),
    );
    await waitFor(() =>
      expect(usePanel.getState().stack.at(-1)).toMatchObject({
        view: "entity-detail",
        title: "Buy milk",
      }),
    );
    // Read-only: no mutating action anywhere in the resolved payload/click path.
    expect(mockInvoke).not.toHaveBeenCalled();
  });

  it("collapses feed items beyond the cap into a +N more control", async () => {
    mockModulePage.mockResolvedValue(sample);
    mockCalendarFeed.mockResolvedValue(
      Array.from({ length: 5 }, (_, i) => ({ ...FEED_ITEM, id: `t${i}`, ref_id: `t${i}`, title: `Task ${i}` })),
    );
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    expect(await screen.findByText("+2 more")).toBeInTheDocument();
  });

  it("still renders events when the calendar-feed call fails (module-down tolerance)", async () => {
    mockModulePage.mockResolvedValue(sample);
    mockCalendarFeed.mockRejectedValue(new Error("tasks module unreachable"));
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    expect(await screen.findByText("Standup")).toBeInTheDocument();
  });

  it("renders no feed chips when nothing is due in the visible window", async () => {
    mockModulePage.mockResolvedValue(sample);
    mockCalendarFeed.mockResolvedValue([]);
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    await screen.findByText("Standup");
    expect(screen.queryByText("Buy milk")).not.toBeInTheDocument();
  });
});

// The week view is now an hourly day-grid (#631): events placed by time, a pinned all-day
// strip, and drag-to-move that persists through the event's own update action (#208/ADR-0034).
describe("Week grid (#631)", () => {
  // A timed event carrying the editable-calendar Edit/Delete actions the module supplies (#208).
  const EDITABLE_EVENT = {
    id: "e1",
    title: "Standup",
    start: "2026-06-15T09:00:00",
    end: "2026-06-15T09:30:00",
    provider: "local",
    calendar_id: "local",
    actions: [
      {
        tool: "calendar_update_event",
        label: "Edit",
        form: true,
        args: { event_id: "e1", calendar_id: "local" },
        fields: ["title", "all_day", "start", "end", "location", "description"],
        form_values: {},
      },
      {
        tool: "calendar_delete_event",
        label: "Delete",
        intent: "danger",
        args: { event_id: "e1", calendar_id: "local" },
        confirm: "Delete 'Standup'? This can't be undone.",
      },
    ],
  };

  const showWeek = async () => {
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    // The toolbar (with the view switch) renders before data; switch to week, then each test
    // awaits its own event text, which only appears once the page has loaded.
    fireEvent.click(await screen.findByRole("button", { name: "Week" }));
  };

  it("places a timed event as a positioned box sized by its duration", async () => {
    mockModulePage.mockResolvedValue({ ...sample, events: [EDITABLE_EVENT] });
    await showWeek();

    const box = (await screen.findByText("Standup")).closest("button")!;
    // 09:00 → top = 9 * HOUR_HEIGHT; 30-minute duration → height = HOUR_HEIGHT / 2.
    expect(box.style.top).toBe(`${9 * HOUR_HEIGHT}px`);
    expect(box.style.height).toBe(`${HOUR_HEIGHT / 2}px`);
  });

  it("persists a dragged move through the event's update action, one hour later", async () => {
    mockModulePage.mockResolvedValue({ ...sample, events: [EDITABLE_EVENT] });
    mockInvoke.mockResolvedValue({});
    await showWeek();

    const box = (await screen.findByText("Standup")).closest("button")!;
    fireEvent.pointerDown(box, { clientY: 200, button: 0 });
    fireEvent.pointerMove(window, { clientY: 200 + HOUR_HEIGHT }); // one hour-row down → +60 min
    fireEvent.pointerUp(window, { clientY: 200 + HOUR_HEIGHT });

    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith(
        "calendar",
        "calendar_update_event",
        expect.objectContaining({ event_id: "e1", calendar_id: "local" }),
      ),
    );
    const call = mockInvoke.mock.calls.find((c) => c[1] === "calendar_update_event")!;
    const args = call[2] as { start: string; end: string };
    // Both endpoints shifted +1h, preserving the 30-minute duration (asserted in local terms,
    // so it holds regardless of the runner's timezone).
    expect(new Date(args.start).getTime() - new Date("2026-06-15T09:00:00").getTime()).toBe(3_600_000);
    expect(new Date(args.end).getTime() - new Date("2026-06-15T09:30:00").getTime()).toBe(3_600_000);
  });

  it("opens the event detail on a plain click (no drag)", async () => {
    mockModulePage.mockResolvedValue({ ...sample, events: [EDITABLE_EVENT] });
    await showWeek();

    fireEvent.click((await screen.findByText("Standup")).closest("button")!);
    expect(await screen.findByRole("dialog", { name: "Standup" })).toBeInTheDocument();
  });

  it("does not move a read-only event (no update action) on drag", async () => {
    // Same event without any actions → not draggable; a drag must not call any tool.
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [{ ...EDITABLE_EVENT, actions: [] }],
    });
    mockInvoke.mockResolvedValue({});
    await showWeek();

    const box = (await screen.findByText("Standup")).closest("button")!;
    fireEvent.pointerDown(box, { clientY: 200, button: 0 });
    fireEvent.pointerMove(window, { clientY: 200 + HOUR_HEIGHT });
    fireEvent.pointerUp(window, { clientY: 200 + HOUR_HEIGHT });

    expect(mockInvoke).not.toHaveBeenCalled();
  });

  it("pins an all-day event in the all-day strip, not the hour grid", async () => {
    mockModulePage.mockResolvedValue({
      ...sample,
      events: [
        {
          id: "ad1",
          title: "Conference",
          start: "2026-06-15",
          end: "2026-06-17",
          all_day: true,
          provider: "local",
        },
      ],
    });
    await showWeek();

    // A 2-day span (end exclusive on the 17th) shows a chip on each day it covers, 15th + 16th.
    const chips = await screen.findAllByText("Conference");
    expect(chips).toHaveLength(2);
    const chip = chips[0].closest("button")!;
    // A strip chip is not absolutely positioned into the hour grid (no top/height styles).
    expect(chip.style.top).toBe("");
    expect(chip.style.height).toBe("");
    // The "All-day" gutter label is present, confirming the strip rendered.
    expect(screen.getByText("All-day")).toBeInTheDocument();
  });
});
