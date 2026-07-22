import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationsScreen } from "@/screens/NotificationsScreen";
import type { NotificationCenterItem } from "@/lib/contracts";
import { usePanel } from "@/stores/panel";

const mockNotifications = vi.fn();
const mockPrefs = vi.fn();
const mockMarkRead = vi.fn();
const mockMarkAllRead = vi.fn();
const mockResolveEntity = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    notifications: (...a: unknown[]) => mockNotifications(...a),
    pushPrefs: (...a: unknown[]) => mockPrefs(...a),
    markNotificationRead: (...a: unknown[]) => mockMarkRead(...a),
    markAllNotificationsRead: (...a: unknown[]) => mockMarkAllRead(...a),
    resolveEntity: (...a: unknown[]) => mockResolveEntity(...a),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const KNOWN_CATEGORIES = [
  { id: "mail", label: "Mail" },
  { id: "tasks", label: "Tasks" },
];

function item(overrides: Partial<NotificationCenterItem> = {}): NotificationCenterItem {
  return {
    id: "n1",
    category: "mail",
    title: "New mail from Alice",
    body: "Hey, are we still on for lunch?",
    deep_link: null,
    entity_ref: null,
    automation_id: null,
    created_at: "2026-07-17T09:00:00Z",
    read_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockNotifications.mockReset().mockResolvedValue([]);
  mockPrefs.mockReset().mockResolvedValue({
    categories: {},
    known_categories: KNOWN_CATEGORIES,
    quiet_hours_enabled: false,
    quiet_hours_start: "22:00",
    quiet_hours_end: "07:00",
  });
  mockMarkRead.mockReset().mockResolvedValue(undefined);
  mockMarkAllRead.mockReset().mockResolvedValue({ marked: 0 });
  mockResolveEntity.mockReset();
  usePanel.getState().close();
});

describe("NotificationsScreen (#671, ADR-0102)", () => {
  it("shows an empty state when there is nothing", async () => {
    render(<NotificationsScreen />, { wrapper });
    expect(await screen.findByText(/nothing here yet/i)).toBeInTheDocument();
  });

  it("lists notifications with category, title, and body", async () => {
    mockNotifications.mockResolvedValue([item()]);
    render(<NotificationsScreen />, { wrapper });
    expect(await screen.findByText("New mail from Alice")).toBeInTheDocument();
    expect(screen.getByText("Hey, are we still on for lunch?")).toBeInTheDocument();
    expect(screen.getByText("mail")).toBeInTheDocument();
  });

  it("shows an unread row as clickable and a read row as not", async () => {
    mockNotifications.mockResolvedValue([
      item({ id: "unread", title: "Unread one", read_at: null }),
      item({ id: "read", title: "Read one", read_at: "2026-07-17T09:05:00Z" }),
    ]);
    render(<NotificationsScreen />, { wrapper });
    const unreadRow = (await screen.findByText("Unread one")).closest("li");
    const readRow = screen.getByText("Read one").closest("li");
    expect(unreadRow?.className).toContain("cursor-pointer");
    expect(readRow?.className).not.toContain("cursor-pointer");
  });

  it("clicking an unread row marks it read", async () => {
    mockNotifications.mockResolvedValue([item({ id: "n1", read_at: null })]);
    render(<NotificationsScreen />, { wrapper });
    const row = (await screen.findByText("New mail from Alice")).closest("li") as HTMLElement;
    fireEvent.click(row);
    await waitFor(() => expect(mockMarkRead).toHaveBeenCalledWith("n1"));
  });

  it("clicking an already-read row does not call markNotificationRead again", async () => {
    mockNotifications.mockResolvedValue([
      item({ id: "n1", read_at: "2026-07-17T09:05:00Z" }),
    ]);
    render(<NotificationsScreen />, { wrapper });
    const row = (await screen.findByText("New mail from Alice")).closest("li") as HTMLElement;
    fireEvent.click(row);
    expect(mockMarkRead).not.toHaveBeenCalled();
  });

  it("shows Mark all read only when something is unread, and calls the API", async () => {
    mockNotifications.mockResolvedValue([item({ read_at: null })]);
    render(<NotificationsScreen />, { wrapper });
    const button = await screen.findByRole("button", { name: /mark all read/i });
    fireEvent.click(button);
    await waitFor(() => expect(mockMarkAllRead).toHaveBeenCalled());
  });

  it("hides Mark all read when everything is already read", async () => {
    mockNotifications.mockResolvedValue([item({ read_at: "2026-07-17T09:05:00Z" })]);
    render(<NotificationsScreen />, { wrapper });
    await screen.findByText("New mail from Alice");
    expect(screen.queryByRole("button", { name: /mark all read/i })).not.toBeInTheDocument();
  });

  it("renders one filter option per known category, from the shared push-prefs taxonomy", async () => {
    render(<NotificationsScreen />, { wrapper });
    const select = screen.getByLabelText(/filter by category/i) as HTMLSelectElement;
    // Wait for the fetched categories to actually render as <option>s, not just for the
    // mock to have been called — a query being called says nothing about whether it has
    // resolved and re-rendered yet.
    await within(select).findByRole("option", { name: "Tasks" });
    const optionLabels = Array.from(select.options).map((o) => o.textContent);
    expect(optionLabels).toEqual(["All categories", "Mail", "Tasks"]);
  });

  it("changing the category filter re-queries with that category", async () => {
    render(<NotificationsScreen />, { wrapper });
    const select = screen.getByLabelText(/filter by category/i) as HTMLSelectElement;
    // The "tasks" option must exist before fireEvent.change can select it — setting a
    // <select>'s value to one with no matching <option> is a silent no-op in jsdom.
    await within(select).findByRole("option", { name: "Tasks" });
    mockNotifications.mockClear();
    fireEvent.change(select, { target: { value: "tasks" } });
    await waitFor(() =>
      expect(mockNotifications).toHaveBeenCalledWith({ category: "tasks", unreadOnly: false }),
    );
  });

  it("toggling unread-only re-queries with unreadOnly true", async () => {
    render(<NotificationsScreen />, { wrapper });
    await waitFor(() => expect(mockNotifications).toHaveBeenCalled());
    mockNotifications.mockClear();
    fireEvent.click(screen.getByRole("switch", { name: /show unread only/i }));
    await waitFor(() =>
      expect(mockNotifications).toHaveBeenCalledWith({ category: undefined, unreadOnly: true }),
    );
  });

  it("renders an entity-ref chip when the notification carries one", async () => {
    mockNotifications.mockResolvedValue([
      item({
        entity_ref: { ref_id: "e1", module: "mail", kind: "thread", title: "Lunch plans" },
      }),
    ]);
    render(<NotificationsScreen />, { wrapper });
    expect(await screen.findByRole("button", { name: /lunch plans/i })).toBeInTheDocument();
  });

  it("renders an Open link when the notification carries a deep link", async () => {
    mockNotifications.mockResolvedValue([item({ deep_link: "/m/mail/e1" })]);
    render(<NotificationsScreen />, { wrapper });
    const link = await screen.findByRole("link", { name: /open/i });
    expect(link).toHaveAttribute("href", "/m/mail/e1");
  });

  it("shows a load error without crashing", async () => {
    mockNotifications.mockRejectedValue(new Error("unreachable"));
    render(<NotificationsScreen />, { wrapper });
    expect(await screen.findByText(/could not load notifications/i)).toBeInTheDocument();
  });
});
