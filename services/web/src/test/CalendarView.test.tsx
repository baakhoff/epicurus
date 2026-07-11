import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

describe("Slot-click create (#473)", () => {
  it("opens the existing create form pre-filled with the clicked day, all-day on", async () => {
    mockModules.mockResolvedValue(CALENDAR_MODULE_WITH_CREATE_SCHEMA);
    mockModulePage.mockResolvedValue({
      ...sample,
      actions: [
        { tool: "calendar_create_event", label: "New event", form: true, form_values: {} },
      ],
    });
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");

    const cell = findEmptyDayCell(container);
    const dayNum = cell.querySelector("span.rounded-full")!.textContent!.padStart(2, "0");
    fireEvent.click(cell);

    const startInput = (await screen.findByLabelText("Start")) as HTMLInputElement;
    const endInput = screen.getByLabelText("End") as HTMLInputElement;
    expect(startInput).toHaveAttribute("type", "date"); // date_toggle collapsed it (#252/#473)
    expect(startInput.value.endsWith(`-${dayNum}`)).toBe(true);
    expect(endInput.value.endsWith(`-${dayNum}`)).toBe(false); // exclusive end = start + 1 day

    const allDay = screen.getByRole("switch", { name: "All day" });
    expect(allDay).toHaveAttribute("aria-checked", "true");
  });

  it("submits the seeded date without the operator re-typing it", async () => {
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

    const cell = findEmptyDayCell(container);
    const dayNum = cell.querySelector("span.rounded-full")!.textContent!.padStart(2, "0");
    fireEvent.click(cell);
    const startInput = (await screen.findByLabelText("Start")) as HTMLInputElement;
    const seededStart = startInput.value;
    expect(seededStart.endsWith(`-${dayNum}`)).toBe(true);

    // Scoped to the open sheet: its submit button shares "New event" with the toolbar's
    // own (separate, still-closed) trigger button, so an unscoped query would be ambiguous.
    const sheet = screen.getByRole("dialog", { name: "New event" });
    fireEvent.click(within(sheet).getByRole("button", { name: "New event" }));
    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith(
        "calendar",
        "calendar_create_event",
        expect.objectContaining({
          calendar_id: "local", // the existing default (#473 must not clobber it)
          start: seededStart,
          all_day: true,
        }),
      ),
    );
  });

  it("does not open the create form when an event chip is clicked", async () => {
    mockModules.mockResolvedValue(CALENDAR_MODULE_WITH_CREATE_SCHEMA);
    mockModulePage.mockResolvedValue({
      ...sample,
      actions: [
        { tool: "calendar_create_event", label: "New event", form: true, form_values: {} },
      ],
    });
    render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });

    fireEvent.click(await screen.findByText("Standup"));
    expect(await screen.findByRole("dialog", { name: "Standup" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Start")).not.toBeInTheDocument();
  });

  it('does not open the create form when the "+N more" button is clicked', async () => {
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

    fireEvent.click(await screen.findByText("+2 more"));
    expect(await screen.findByRole("dialog")).toBeInTheDocument(); // opened event detail…
    expect(screen.queryByLabelText("Start")).not.toBeInTheDocument(); // …not the create form
  });

  it("leaves day cells inert when the page declares no create action", async () => {
    mockModulePage.mockResolvedValue(sample); // no `actions` at all
    const { container } = render(<CalendarView module="calendar" pageId="calendar" />, { wrapper });
    await screen.findByText("Standup");

    fireEvent.click(findEmptyDayCell(container));
    expect(screen.queryByLabelText("Start")).not.toBeInTheDocument();
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
