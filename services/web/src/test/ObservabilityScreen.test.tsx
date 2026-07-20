/**
 * Tests for the Observability screen's two consoles and the tab strip between them.
 *
 * The screen had no tests before the Events tab was added, which made extracting the log
 * console into its own component a change with no net underneath it. These cover both
 * feeds — so the extraction is pinned, not just the new tab.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Automation, AutomationRun, LogEntry, ModuleEvent } from "@/lib/contracts";

let logEntries: LogEntry[] = [];
let moduleEvents: ModuleEvent[] = [];
let automationRuns: AutomationRun[] = [];
let automationRows: Automation[] = [];
const logCalls: { level?: string; service?: string }[] = [];
const eventCalls: { module?: string; type?: string }[] = [];
const runCalls: { automationId?: string; outcome?: string }[] = [];
/** Streams that never end, mirroring the real feeds (which stay open for live entries). */
let hold = true;

vi.mock("@/lib/api", () => ({
  api: {
    readiness: vi.fn().mockResolvedValue({
      ready: true,
      power: "idle",
      components: [{ name: "model", ready: true, detail: "qwen2.5:7b" }],
    }),
    automations: vi.fn(() => Promise.resolve(automationRows)),
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
  runStream: async function* (automationId?: string, outcome?: string) {
    runCalls.push({ automationId, outcome });
    // Filter like the server does (see eventStream above).
    for (const run of automationRuns) {
      if (automationId && run.automation_id !== automationId) continue;
      if (outcome && run.outcome !== outcome) continue;
      yield run;
    }
    while (hold) await new Promise((r) => setTimeout(r, 5));
  },
}));

import { ObservabilityScreen } from "@/screens/ObservabilityScreen";

/** The runs tab's entity-ref chips need a query client; the other tabs don't mind one. */
function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ObservabilityScreen />
    </QueryClientProvider>,
  );
}

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

function automationRun(overrides: Partial<AutomationRun> = {}): AutomationRun {
  return {
    id: "r1",
    automation_id: "a1",
    started_at: "2026-07-20T09:00:00Z",
    trigger_refs: [],
    filter_verdict: "matched",
    model: "qwen2.5:7b",
    prompt_tokens: 812,
    completion_tokens: 96,
    duration_ms: 4210,
    outcome: "ok",
    error: null,
    output: "An invoice from Acme arrived.",
    sinks_fired: ["chat"],
    trigger_entity_refs: [],
    ...overrides,
  };
}

function automation(overrides: Partial<Automation> = {}): Automation {
  return {
    id: "a1",
    name: "Tell me about invoices",
    enabled: true,
    source: "user",
    event_trigger: {
      module: "mail",
      event_type: "mail.received",
      matchers: [],
      window_start_hour: null,
      window_end_hour: null,
    },
    schedule_trigger: null,
    prompt: "Summarize the invoice.",
    model: null,
    autonomy: "notify",
    sinks: ["chat"],
    chat_mode: "rolling",
    rate_cap_per_hour: 0,
    digest_window_minutes: 0,
    created_at: "2026-07-19T08:00:00Z",
    last_run_at: null,
    last_status: null,
    allowed_tool_classes: ["read"],
    ...overrides,
  };
}

beforeEach(() => {
  logEntries = [];
  moduleEvents = [];
  automationRuns = [];
  automationRows = [];
  logCalls.length = 0;
  eventCalls.length = 0;
  runCalls.length = 0;
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

    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Automation runs" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await userEvent.keyboard("{ArrowRight}"); // wraps back around
    expect(screen.getByRole("tab", { name: "Logs" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows system health", async () => {
    render(<ObservabilityScreen />);
    expect(await screen.findByText("qwen2.5:7b")).toBeInTheDocument();
  });
});

describe("Automation runs console (#669)", () => {
  async function openRunsTab() {
    renderScreen();
    await userEvent.click(screen.getByRole("tab", { name: "Automation runs" }));
  }

  it("streams runs and names the automation from the automations list", async () => {
    automationRows = [automation({ id: "a1", name: "Tell me about invoices" })];
    automationRuns = [automationRun({ automation_id: "a1" })];
    await openRunsTab();

    // Scoped to the list — the automation filter's <option> carries the same text.
    const list = within(await screen.findByLabelText("Automation runs console"));
    expect(await list.findByText("Tell me about invoices")).toBeInTheDocument();
    expect(list.getByText("matched")).toBeInTheDocument();
    expect(list.getByText("ok")).toBeInTheDocument();
    expect(list.getByText("812+96 tok")).toBeInTheDocument();
    expect(list.getByText("4210 ms")).toBeInTheDocument();
    expect(list.getByText("→ chat")).toBeInTheDocument();
  });

  it("shows a skipped run's why as loudly as a real run", async () => {
    // The tab exists so rate caps and pauses are visible, not inferred from silence.
    automationRuns = [
      automationRun({
        id: "r-skip",
        outcome: "skipped",
        error: "rate cap reached (4/hour)",
        model: null,
        prompt_tokens: null,
        completion_tokens: null,
        output: "",
        sinks_fired: [],
      }),
    ];
    await openRunsTab();

    const list = within(await screen.findByLabelText("Automation runs console"));
    expect(await list.findByText("skipped")).toBeInTheDocument();
    expect(list.getByText("rate cap reached (4/hour)")).toBeInTheDocument();
  });

  it("re-subscribes with the outcome filter server-side", async () => {
    automationRuns = [automationRun({ id: "r-ok", outcome: "ok" })];
    await openRunsTab();
    await waitFor(() => expect(runCalls.length).toBe(1));
    expect(runCalls[0]).toEqual({ automationId: undefined, outcome: undefined });

    await userEvent.selectOptions(screen.getByLabelText("Outcome filter"), "skipped");
    await waitFor(() => expect(runCalls.at(-1)?.outcome).toBe("skipped"));
    // The previous filter's rows must not linger under the new one.
    expect(screen.queryByText("matched")).not.toBeInTheDocument();
  });

  it("filters by trigger module client-side without tearing down the stream", async () => {
    automationRows = [
      automation({ id: "a1", name: "Invoice watcher" }),
      automation({
        id: "a2",
        name: "Note watcher",
        event_trigger: {
          module: "notes",
          event_type: "notes.note_updated",
          matchers: [],
          window_start_hour: null,
          window_end_hour: null,
        },
      }),
    ];
    automationRuns = [
      automationRun({ id: "r1", automation_id: "a1" }),
      automationRun({ id: "r2", automation_id: "a2" }),
    ];
    await openRunsTab();
    const list = within(await screen.findByLabelText("Automation runs console"));
    expect(await list.findByText("Invoice watcher")).toBeInTheDocument();
    expect(list.getByText("Note watcher")).toBeInTheDocument();
    const subscriptions = runCalls.length;

    await userEvent.selectOptions(screen.getByLabelText("Trigger module filter"), "notes");

    expect(list.queryByText("Invoice watcher")).not.toBeInTheDocument();
    expect(list.getByText("Note watcher")).toBeInTheDocument();
    expect(runCalls.length).toBe(subscriptions); // no re-subscribe for a client-side view
  });

  it("expands a run's output on demand", async () => {
    automationRuns = [automationRun({ output: "An invoice from Acme arrived." })];
    await openRunsTab();

    const toggle = await screen.findByLabelText("Expand output");
    expect(screen.queryByText("An invoice from Acme arrived.")).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(screen.getByText("An invoice from Acme arrived.")).toBeInTheDocument();
  });

  it("renders the triggering events' entity-ref chips", async () => {
    automationRuns = [
      automationRun({
        trigger_entity_refs: [
          { ref_id: "m1", module: "mail", kind: "message", title: "Re: invoice" },
        ],
      }),
    ];
    await openRunsTab();
    // The chip renders its title in the pill and again in the hover-card body.
    expect((await screen.findAllByText("Re: invoice")).length).toBeGreaterThan(0);
  });

  it("falls back to the automation id when the list does not know it", async () => {
    automationRuns = [automationRun({ automation_id: "deadbeefcafe" })];
    await openRunsTab();
    expect(await screen.findByText("deadbeef")).toBeInTheDocument();
  });
});
