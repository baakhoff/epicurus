/** Per-event alerts (#732): declared-event catalog rows + custom (module, event_type)
 *  entries. Since #797 each row renders two coupled switches — Alert (the master; an enabled
 *  alert always lands in the notification center) and Push (also deliver to devices) — over
 *  the unchanged `{push, center}` wire contract. Switch order per row: [0] Alert, [1] Push. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventAlertsCard } from "@/components/EventAlertsCard";
import type { EventSubscription, ModuleSnapshot } from "@/lib/contracts";

const mockModules = vi.fn();
const mockEventSubscriptions = vi.fn();
const mockSetEventSubscription = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modules: (...a: unknown[]) => mockModules(...a),
    eventSubscriptions: (...a: unknown[]) => mockEventSubscriptions(...a),
    setEventSubscription: (...a: unknown[]) => mockSetEventSubscription(...a),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function moduleSnapshot(
  name: string,
  events: string[],
  overrides: Partial<ModuleSnapshot["manifest"]> = {},
): ModuleSnapshot {
  return {
    manifest: {
      name,
      version: "0.1.0",
      description: "",
      contract_version: "0.1",
      tags: [],
      tools: [],
      events_emitted: events.map((e) => ({ subject: `events.${e}`, description: "" })),
      events_consumed: [],
      config: [],
      secrets: [],
      ui: null,
      pages: [],
      resolver: false,
      attachable: false,
      required_models: [],
      collections: null,
      oauth_scopes: {},
      docs_url: null,
      reindexable: false,
      ...overrides,
    },
    status: { healthy: true, version: null, error: null },
  } as unknown as ModuleSnapshot;
}

function sub(overrides: Partial<EventSubscription> = {}): EventSubscription {
  return { module: "mail", event_type: "mail.received", push: true, center: true, ...overrides };
}

beforeEach(() => {
  mockModules.mockReset().mockResolvedValue([
    moduleSnapshot("mail", ["mail.received", "mail.sent"]),
    moduleSnapshot("tasks", ["tasks.due"]),
  ]);
  mockEventSubscriptions.mockReset().mockResolvedValue([]);
  mockSetEventSubscription.mockReset().mockResolvedValue(sub());
});

describe("EventAlertsCard (#732)", () => {
  it("renders one row per declared event, grouped by module", async () => {
    render(<EventAlertsCard />, { wrapper });
    expect(await screen.findByText("mail")).toBeInTheDocument();
    expect(screen.getByText("mail.received")).toBeInTheDocument();
    expect(screen.getByText("mail.sent")).toBeInTheDocument();
    expect(screen.getByText("tasks")).toBeInTheDocument();
    expect(screen.getByText("tasks.due")).toBeInTheDocument();
  });

  it("defaults every declared event's switches to off when unsubscribed", async () => {
    render(<EventAlertsCard />, { wrapper });
    const row = (await screen.findByText("mail.received")).closest("div") as HTMLElement;
    const switches = within(row).getAllByRole("switch");
    expect(switches).toHaveLength(2);
    expect(switches[0]).toHaveAttribute("aria-checked", "false");
    expect(switches[1]).toHaveAttribute("aria-checked", "false");
  });

  it("reads a center-only subscription as alert-on, push-off", async () => {
    mockEventSubscriptions.mockResolvedValue([
      sub({ module: "mail", event_type: "mail.received", push: false, center: true }),
    ]);
    render(<EventAlertsCard />, { wrapper });
    const row = (await screen.findByText("mail.received")).closest("div") as HTMLElement;
    const switches = within(row).getAllByRole("switch");
    expect(switches[0]).toHaveAttribute("aria-checked", "true"); // Alert
    expect(switches[1]).toHaveAttribute("aria-checked", "false"); // Push
  });

  it("reads a legacy push-only row as alert-on too — either stored flag means the alert is on", async () => {
    mockEventSubscriptions.mockResolvedValue([
      sub({ module: "mail", event_type: "mail.received", push: true, center: false }),
    ]);
    render(<EventAlertsCard />, { wrapper });
    const row = (await screen.findByText("mail.received")).closest("div") as HTMLElement;
    const switches = within(row).getAllByRole("switch");
    expect(switches[0]).toHaveAttribute("aria-checked", "true");
    expect(switches[1]).toHaveAttribute("aria-checked", "true");
  });

  it("turning the alert on subscribes with push on by default", async () => {
    render(<EventAlertsCard />, { wrapper });
    const row = (await screen.findByText("mail.received")).closest("div") as HTMLElement;
    fireEvent.click(within(row).getAllByRole("switch")[0]);

    await waitFor(() =>
      expect(mockSetEventSubscription).toHaveBeenCalledWith({
        module: "mail",
        event_type: "mail.received",
        push: true,
        center: true,
      }),
    );
  });

  it("turning the alert off sends both channels off, which deletes the row", async () => {
    mockEventSubscriptions.mockResolvedValue([
      sub({ module: "mail", event_type: "mail.received", push: true, center: true }),
    ]);
    render(<EventAlertsCard />, { wrapper });
    const row = (await screen.findByText("mail.received")).closest("div") as HTMLElement;
    fireEvent.click(within(row).getAllByRole("switch")[0]);

    await waitFor(() =>
      expect(mockSetEventSubscription).toHaveBeenCalledWith({
        module: "mail",
        event_type: "mail.received",
        push: false,
        center: false,
      }),
    );
  });

  it("turning push off keeps the alert on as center-only", async () => {
    mockEventSubscriptions.mockResolvedValue([
      sub({ module: "tasks", event_type: "tasks.due", push: true, center: true }),
    ]);
    render(<EventAlertsCard />, { wrapper });
    const row = (await screen.findByText("tasks.due")).closest("div") as HTMLElement;
    fireEvent.click(within(row).getAllByRole("switch")[1]);

    await waitFor(() =>
      expect(mockSetEventSubscription).toHaveBeenCalledWith({
        module: "tasks",
        event_type: "tasks.due",
        push: false,
        center: true,
      }),
    );
  });

  it("turning push on from fully-off enables the alert with it", async () => {
    render(<EventAlertsCard />, { wrapper });
    const row = (await screen.findByText("tasks.due")).closest("div") as HTMLElement;
    fireEvent.click(within(row).getAllByRole("switch")[1]);

    await waitFor(() =>
      expect(mockSetEventSubscription).toHaveBeenCalledWith({
        module: "tasks",
        event_type: "tasks.due",
        push: true,
        center: true,
      }),
    );
  });

  it("shows an empty state when no module declares any events", async () => {
    mockModules.mockResolvedValue([]);
    render(<EventAlertsCard />, { wrapper });
    expect(await screen.findByText(/no modules declare any events/i)).toBeInTheDocument();
  });

  it("lists a subscribed event outside the declared catalog under Custom", async () => {
    mockEventSubscriptions.mockResolvedValue([
      sub({ module: "echo", event_type: "echo.pinged", push: true, center: true }),
    ]);
    render(<EventAlertsCard />, { wrapper });
    expect(await screen.findByText("echo · echo.pinged")).toBeInTheDocument();
  });

  it("adding a custom module/event subscribes with both channels on", async () => {
    render(<EventAlertsCard />, { wrapper });
    fireEvent.change(await screen.findByLabelText(/^module$/i), {
      target: { value: "echo" },
    });
    fireEvent.change(screen.getByLabelText(/event type/i), {
      target: { value: "echo.pinged" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

    await waitFor(() =>
      expect(mockSetEventSubscription).toHaveBeenCalledWith({
        module: "echo",
        event_type: "echo.pinged",
        push: true,
        center: true,
      }),
    );
  });

  it("disables Add until both module and event type are filled in", async () => {
    render(<EventAlertsCard />, { wrapper });
    const addButton = await screen.findByRole("button", { name: /^add$/i });
    expect(addButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/^module$/i), { target: { value: "echo" } });
    expect(addButton).toBeDisabled(); // event type still blank
  });

  it("shows an error state when loading subscriptions fails", async () => {
    mockEventSubscriptions.mockRejectedValue(new Error("boom"));
    render(<EventAlertsCard />, { wrapper });
    expect(await screen.findByText(/could not load event alerts/i)).toBeInTheDocument();
  });
});
