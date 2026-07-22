/**
 * Tests for the Observability screen's two consoles and the tab strip between them.
 *
 * The screen had no tests before the Events tab was added, which made extracting the log
 * console into its own component a change with no net underneath it. These cover both
 * feeds — so the extraction is pinned, not just the new tab.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LogEntry, ModuleEvent } from "@/lib/contracts";

let logEntries: LogEntry[] = [];
let moduleEvents: ModuleEvent[] = [];
const logCalls: { level?: string; service?: string }[] = [];
const eventCalls: { module?: string; type?: string }[] = [];
/** Streams that never end, mirroring the real feeds (which stay open for live entries). */
let hold = true;

vi.mock("@/lib/api", () => ({
  api: {
    readiness: vi.fn().mockResolvedValue({
      ready: true,
      power: "idle",
      components: [{ name: "model", ready: true, detail: "qwen2.5:7b" }],
    }),
  },
  logStream: async function* (level?: string, service?: string) {
    logCalls.push({ level, service });
    for (const entry of logEntries) yield entry;
    while (hold) await new Promise((r) => setTimeout(r, 5));
  },
  eventStream: async function* (module?: string, type?: string) {
    eventCalls.push({ module, type });
    // Filter like the server does — a mock that ignores its own arguments would re-yield
    // everything on re-subscribe and quietly hide a broken filter.
    for (const event of moduleEvents) {
      if (module && event.module !== module) continue;
      if (type && event.type !== type) continue;
      yield event;
    }
    while (hold) await new Promise((r) => setTimeout(r, 5));
  },
}));

import { ObservabilityScreen } from "@/screens/ObservabilityScreen";

function logEntry(overrides: Partial<LogEntry> = {}): LogEntry {
  return {
    ts: "2026-07-17T12:00:00.000Z",
    level: "info",
    service: "core-app",
    message: "core runtime ready",
    context: {},
    ...overrides,
  };
}

function moduleEvent(overrides: Partial<ModuleEvent> = {}): ModuleEvent {
  return {
    id: 1,
    tenant: "local",
    module: "echo",
    type: "echo.pinged",
    occurred_at: "2026-07-17T12:00:00Z",
    received_at: "2026-07-17T12:00:01Z",
    dedup_key: "k1",
    entity_ref: null,
    payload: {},
    schema_version: 1,
    ...overrides,
  };
}

beforeEach(() => {
  logEntries = [];
  moduleEvents = [];
  logCalls.length = 0;
  eventCalls.length = 0;
  hold = true;
});

afterEach(() => {
  hold = false; // let the held generators finish so they do not outlive the test
});

describe("ObservabilityScreen", () => {
  it("opens on the Logs tab and streams log entries", async () => {
    logEntries = [logEntry({ message: "core runtime ready" })];
    render(<ObservabilityScreen />);

    expect(await screen.findByText("core runtime ready")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Logs" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "false");
  });

  it("switches to the Events tab and streams module events", async () => {
    moduleEvents = [moduleEvent({ type: "echo.pinged", module: "echo" })];
    render(<ObservabilityScreen />);

    await userEvent.click(screen.getByRole("tab", { name: "Events" }));

    expect(await screen.findByText("echo.pinged")).toBeInTheDocument();
    expect(screen.getByLabelText("Events console")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
  });

  it("only subscribes to the visible tab's feed", async () => {
    // A hidden tab holding an SSE connection open is a live subscription nobody reads.
    render(<ObservabilityScreen />);
    await waitFor(() => expect(logCalls.length).toBe(1));
    expect(eventCalls).toHaveLength(0);

    await userEvent.click(screen.getByRole("tab", { name: "Events" }));
    await waitFor(() => expect(eventCalls.length).toBe(1));
    expect(screen.queryByLabelText("Log console")).not.toBeInTheDocument();
  });

  it("renders an event's module, type, and entity title", async () => {
    moduleEvents = [
      moduleEvent({
        module: "mail",
        type: "mail.received",
        entity_ref: { ref_id: "m1", module: "mail", kind: "message", title: "Re: lunch" },
      }),
    ];
    render(<ObservabilityScreen />);
    await userEvent.click(screen.getByRole("tab", { name: "Events" }));

    expect(await screen.findByText("mail.received")).toBeInTheDocument();
    expect(screen.getByText("mail")).toBeInTheDocument();
    expect(screen.getByText("Re: lunch")).toBeInTheDocument();
  });

  it("expands an event payload on demand", async () => {
    moduleEvents = [moduleEvent({ payload: { note: "hello" } })];
    render(<ObservabilityScreen />);
    await userEvent.click(screen.getByRole("tab", { name: "Events" }));

    const toggle = await screen.findByLabelText("Expand payload");
    expect(screen.queryByText(/"note": "hello"/)).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(screen.getByText(/"note": "hello"/)).toBeInTheDocument();
  });

  it("offers no payload toggle when there is nothing to show", async () => {
    moduleEvents = [moduleEvent({ payload: {} })];
    render(<ObservabilityScreen />);
    await userEvent.click(screen.getByRole("tab", { name: "Events" }));

    await screen.findByText("echo.pinged");
    expect(screen.queryByLabelText("Expand payload")).not.toBeInTheDocument();
  });

  it("re-subscribes with the module filter and clears what was shown", async () => {
    moduleEvents = [moduleEvent({ module: "echo", type: "echo.pinged" })];
    render(<ObservabilityScreen />);
    await userEvent.click(screen.getByRole("tab", { name: "Events" }));
    await screen.findByText("echo.pinged");

    await userEvent.type(screen.getByLabelText("Module filter"), "mail");

    await waitFor(() => expect(eventCalls.at(-1)?.module).toBe("mail"));
    // The previous filter's results must not linger under the new one.
    expect(screen.queryByText("echo.pinged")).not.toBeInTheDocument();
  });

  it("clears the events console on demand", async () => {
    moduleEvents = [moduleEvent()];
    render(<ObservabilityScreen />);
    await userEvent.click(screen.getByRole("tab", { name: "Events" }));
    await screen.findByText("echo.pinged");

    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByText("echo.pinged")).not.toBeInTheDocument();
  });

  it("counts events with the right noun", async () => {
    moduleEvents = [moduleEvent({ id: 1, dedup_key: "a" }), moduleEvent({ id: 2, dedup_key: "b" })];
    render(<ObservabilityScreen />);
    await userEvent.click(screen.getByRole("tab", { name: "Events" }));
    expect(await screen.findByText("2 events")).toBeInTheDocument();
  });

  it("keeps the log console's filters working after the extraction", async () => {
    logEntries = [logEntry()];
    render(<ObservabilityScreen />);
    await waitFor(() => expect(logCalls.length).toBe(1));
    expect(logCalls[0]).toEqual({ level: "info", service: undefined });

    await userEvent.selectOptions(screen.getByLabelText("Minimum log level"), "error");
    await waitFor(() => expect(logCalls.at(-1)?.level).toBe("error"));
  });

  it("moves between tabs with the arrow keys", async () => {
    render(<ObservabilityScreen />);
    const logsTab = screen.getByRole("tab", { name: "Logs" });
    logsTab.focus();

    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");

    await userEvent.keyboard("{ArrowRight}"); // wraps back around
    expect(screen.getByRole("tab", { name: "Logs" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows system health", async () => {
    render(<ObservabilityScreen />);
    expect(await screen.findByText("qwen2.5:7b")).toBeInTheDocument();
  });
});
