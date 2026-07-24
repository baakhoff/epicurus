/**
 * The Automations page (#668): list, kill switch, editor, templates, run history.
 *
 * The engine is mocked at the api module boundary (the repo pattern); what is exercised
 * is the page's own behavior — the words the list renders, which endpoint each control
 * hits, and that a template instantiation goes through the ordinary create path with its
 * provenance attached.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { Automation, AutomationRun, AutomationTemplate } from "@/lib/contracts";

let automationRows: Automation[] = [];
let templates: AutomationTemplate[] = [];
let runs: AutomationRun[] = [];
let killSwitch = { halted: false };

vi.mock("@/lib/api", () => ({
  api: {
    automations: vi.fn(() => Promise.resolve(automationRows)),
    automationRuns: vi.fn(() => Promise.resolve(runs)),
    automationVocabulary: vi.fn(() =>
      Promise.resolve({
        autonomy_levels: ["notify", "propose", "act", "silent_act"],
        sinks: ["push", "chat", "notes", "kb"],
        matcher_ops: ["eq", "ne", "contains", "exists", "gt", "lt"],
      }),
    ),
    automationTemplates: vi.fn(() => Promise.resolve(templates)),
    createAutomation: vi.fn((body: unknown) =>
      Promise.resolve({ ...automation(), ...(body as object), id: "created" }),
    ),
    updateAutomation: vi.fn((id: string, body: unknown) =>
      Promise.resolve({ ...automation(), ...(body as object), id }),
    ),
    setAutomationEnabled: vi.fn(() => Promise.resolve({})),
    deleteAutomation: vi.fn(() => Promise.resolve()),
    runAutomationNow: vi.fn(() => Promise.resolve({})),
    automationKillSwitch: vi.fn(() => Promise.resolve(killSwitch)),
    setAutomationKillSwitch: vi.fn((halted: boolean) => {
      killSwitch = { halted };
      return Promise.resolve(killSwitch);
    }),
    modules: vi.fn(() =>
      Promise.resolve([
        {
          manifest: {
            name: "mail",
            version: "0.14.0",
            description: "",
            contract_version: "0.1",
            tags: [],
            tools: [],
            events_emitted: [
              { subject: "events.mail.received", description: "" },
              { subject: "events.mail.sent", description: "" },
            ],
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
          },
          status: { healthy: true, version: null, error: null },
        },
      ]),
    ),
    models: vi.fn(() => Promise.resolve([{ name: "qwen2.5:7b", loaded: true, hidden: false, capabilities: [], size: null, context_length: null }])),
    savedModels: vi.fn(() => Promise.resolve([])),
  },
}));

import { api } from "@/lib/api";
import { AutomationsScreen, triggerSummary } from "@/screens/AutomationsScreen";

function automation(overrides: Partial<Automation> = {}): Automation {
  return {
    id: "a1",
    name: "Tell me about invoices",
    enabled: true,
    source: "user",
    event_trigger: {
      module: "mail",
      event_type: "mail.received",
      matchers: [{ field: "subject", op: "contains", value: "invoice" }],
      window_start_hour: 9,
      window_end_hour: 17,
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
    last_run_at: "2026-07-20T07:00:00Z",
    last_status: "ok",
    allowed_tool_classes: ["read"],
    ...overrides,
  };
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AutomationsScreen />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  automationRows = [];
  templates = [];
  runs = [];
  killSwitch = { halted: false };
});

describe("triggerSummary", () => {
  it("reads an event trigger in words", () => {
    expect(triggerSummary(automation())).toBe(
      "When mail.received arrives, matching subject contains invoice, between 09:00–17:00",
    );
  });

  it("reads schedules in words", () => {
    expect(
      triggerSummary(
        automation({
          event_trigger: null,
          schedule_trigger: { cadence: "daily", hour: 7, weekday: null },
        }),
      ),
    ).toBe("Daily at 07:00");
    expect(
      triggerSummary(
        automation({
          event_trigger: null,
          schedule_trigger: { cadence: "weekly", hour: 9, weekday: 1 },
        }),
      ),
    ).toBe("Weekly on Tuesday at 09:00");
  });
});

describe("AutomationsScreen", () => {
  it("lists automations with trigger words, autonomy, and last run", async () => {
    automationRows = [automation()];
    renderScreen();

    expect(await screen.findByText("Tell me about invoices")).toBeInTheDocument();
    expect(
      screen.getByText(
        "When mail.received arrives, matching subject contains invoice, between 09:00–17:00",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("notify")).toBeInTheDocument();
    expect(screen.getByText(/Last run:/)).toBeInTheDocument();
  });

  it("toggles an automation without a reload", async () => {
    automationRows = [automation()];
    renderScreen();

    await userEvent.click(
      await screen.findByRole("switch", { name: "Tell me about invoices enabled" }),
    );
    await waitFor(() => expect(api.setAutomationEnabled).toHaveBeenCalledWith("a1", false));
  });

  it("shows and flips the kill switch", async () => {
    renderScreen();
    expect(await screen.findByText("Automations are running")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("switch", { name: "Automations kill switch" }));
    await waitFor(() => expect(api.setAutomationKillSwitch).toHaveBeenCalledWith(true));
  });

  it("creates an automation through the editor", async () => {
    renderScreen();
    await userEvent.click(await screen.findByRole("button", { name: /New automation/ }));

    const dialog = within(screen.getByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("Name"), "Morning brief");
    await userEvent.type(dialog.getByLabelText("Instructions"), "Summarize my day.");
    await userEvent.click(dialog.getByRole("button", { name: "Create automation" }));

    await waitFor(() => expect(api.createAutomation).toHaveBeenCalled());
    const body = vi.mocked(api.createAutomation).mock.calls[0][0];
    expect(body.name).toBe("Morning brief");
    expect(body.schedule_trigger).toEqual({ cadence: "daily", hour: 7, weekday: null });
    expect(body.event_trigger).toBeNull();
    expect(body.enabled).toBe(true);
    // The chat sink is unchecked by default — no chat unless asked (owner rule, #672).
    expect(body.sinks).not.toContain("chat");
  });

  it("configures a notes sink target and sends it on create (#672)", async () => {
    renderScreen();
    await userEvent.click(await screen.findByRole("button", { name: /New automation/ }));

    const dialog = within(screen.getByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("Name"), "Report");
    await userEvent.type(dialog.getByLabelText("Instructions"), "Summarize.");
    // Checking the notes sink reveals its document-target config.
    await userEvent.click(dialog.getByLabelText("sink notes"));
    await userEvent.type(dialog.getByLabelText("Notes document path"), "Reports/Daily");
    await userEvent.click(dialog.getByRole("button", { name: "Create automation" }));

    await waitFor(() => expect(api.createAutomation).toHaveBeenCalled());
    const body = vi.mocked(api.createAutomation).mock.calls[0][0];
    expect(body.sinks).toContain("notes");
    expect(body.notes_target).toEqual({ path_pattern: "Reports/Daily", mode: "append" });
  });

  it("edits an automation through the same editor", async () => {
    automationRows = [automation()];
    renderScreen();
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    const dialog = within(screen.getByRole("dialog"));
    const name = dialog.getByLabelText("Name");
    expect(name).toHaveValue("Tell me about invoices");
    await userEvent.clear(name);
    await userEvent.type(name, "Renamed");
    await userEvent.click(dialog.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(api.updateAutomation).toHaveBeenCalled());
    const [id, body] = vi.mocked(api.updateAutomation).mock.calls[0];
    expect(id).toBe("a1");
    expect(body.name).toBe("Renamed");
    // The trigger survived the round trip untouched.
    expect(body.event_trigger?.event_type).toBe("mail.received");
  });

  it("surfaces a server rejection inline instead of closing", async () => {
    vi.mocked(api.createAutomation).mockRejectedValueOnce(new Error("name must not be blank"));
    renderScreen();
    await userEvent.click(await screen.findByRole("button", { name: /New automation/ }));
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Create automation" }),
    );

    expect(await screen.findByText("name must not be blank")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument(); // still open for the fix
  });

  it("instantiates a template as an independent automation via the editor", async () => {
    templates = [
      {
        module: "echo",
        key: "on-ping",
        name: "Tell me when the spine is pinged",
        description: "The reference template.",
        trigger: { module: "echo", event_type: "echo.pinged" },
        prompt: "Say so in one short sentence.",
        autonomy: "notify",
        sinks: ["chat"],
      },
    ];
    renderScreen();
    await userEvent.click(await screen.findByRole("tab", { name: "Templates" }));
    expect(await screen.findByText("echo")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Use template Tell me when the spine is pinged" }),
    );
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByLabelText("Name")).toHaveValue("Tell me when the spine is pinged");
    await userEvent.click(dialog.getByRole("button", { name: "Create automation" }));

    await waitFor(() => expect(api.createAutomation).toHaveBeenCalled());
    const body = vi.mocked(api.createAutomation).mock.calls[0][0];
    // Provenance travels; the row itself is the operator's own from here on.
    expect(body.source).toBe("template:echo");
    expect(body.event_trigger?.event_type).toBe("echo.pinged");
    expect(body.enabled).toBe(true); // enabled on save — the editor pass IS the review
  });

  it("expands a run history with the observability cross-link", async () => {
    automationRows = [automation()];
    runs = [
      {
        id: "r1",
        automation_id: "a1",
        started_at: "2026-07-20T07:00:00Z",
        trigger_refs: [42],
        filter_verdict: "matched",
        model: "qwen2.5:7b",
        prompt_tokens: 10,
        completion_tokens: 5,
        duration_ms: 900,
        outcome: "skipped",
        error: "rate cap reached (4/hour)",
        output: "",
        sinks_fired: [],
        trigger_entity_refs: [],
        artifacts: [],
      },
    ];
    renderScreen();
    await userEvent.click(await screen.findByRole("button", { name: /History/ }));

    expect(await screen.findByText("rate cap reached (4/hour)")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Open in Observability/ });
    expect(link).toHaveAttribute("href", "/observability?tab=runs&automation=a1");
  });

  it("runs an automation on demand", async () => {
    automationRows = [automation()];
    renderScreen();
    await userEvent.click(
      await screen.findByRole("button", { name: "Run Tell me about invoices now" }),
    );
    await waitFor(() => expect(api.runAutomationNow).toHaveBeenCalledWith("a1"));
  });

  it("deletes only after a confirm", async () => {
    automationRows = [automation()];
    renderScreen();
    await userEvent.click(
      await screen.findByRole("button", { name: "Delete Tell me about invoices" }),
    );
    expect(api.deleteAutomation).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteAutomation).toHaveBeenCalledWith("a1"));
  });
});
