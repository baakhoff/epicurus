import { describe, expect, it } from "vitest";

import {
  AgentEvent,
  AgentTurn,
  Attachment,
  BoardData,
  BrowserData,
  CalendarData,
  CalendarEvent,
  CatalogResponse,
  LlmPrefs,
  MessageRecord,
  ModuleSnapshot,
  PageSpec,
  Readiness,
  SystemInfo,
  parseEventDate,
} from "@/lib/contracts";

describe("contracts", () => {
  it("parses every agent stream event shape", () => {
    expect(AgentEvent.parse({ type: "delta", text: "hel" }).text).toBe("hel");
    expect(AgentEvent.parse({ type: "tool", tool: "echo", status: "running" }).tool).toBe("echo");
    const done = AgentEvent.parse({
      type: "done",
      turn: { content: "hi", tools_used: ["echo"], stopped: "completed" },
    });
    expect(done.turn?.tools_used).toEqual(["echo"]);
  });

  it("parses a readiness event leading the stream (ADR-0027)", () => {
    const event = AgentEvent.parse({
      type: "readiness",
      readiness: {
        ready: false,
        power: "idle",
        components: [{ name: "model", ready: false, detail: "llama3.2 · warming" }],
      },
    });
    expect(event.type).toBe("readiness");
    expect(event.readiness?.components[0].name).toBe("model");
  });

  it("parses a standalone readiness snapshot and defaults its detail", () => {
    const readiness = Readiness.parse({
      ready: true,
      power: "active",
      components: [{ name: "modules", ready: true }],
    });
    expect(readiness.components[0].detail).toBe("");
    expect(readiness.power).toBe("active");
  });

  it("rejects an unknown power state in a readiness snapshot", () => {
    expect(() => Readiness.parse({ ready: true, power: "asleep", components: [] })).toThrow();
  });

  it("parses a manifest-driven module snapshot (the ADR-0007 surface)", () => {
    const snapshot = ModuleSnapshot.parse({
      manifest: {
        name: "echo",
        version: "0.1.0",
        tools: [{ name: "echo", description: "", input_schema: { type: "object" } }],
        ui: {
          summary: "echoes",
          config_schema: { type: "object", properties: { greeting: { type: "string" } } },
          actions: [{ tool: "echo", label: "Send an echo" }],
        },
      },
      status: { healthy: true, version: "0.1.0" },
    });
    expect(snapshot.manifest.ui?.actions[0].intent).toBe("default");
    expect(snapshot.manifest.ui?.ui_version).toBe("1");
  });

  it("tolerates a manifest with no UI section (older modules stay valid)", () => {
    const snapshot = ModuleSnapshot.parse({
      manifest: { name: "old", version: "1.0" },
      status: { healthy: false },
    });
    expect(snapshot.manifest.ui).toBeUndefined();
    expect(snapshot.manifest.tools).toEqual([]);
    expect(snapshot.manifest.pages).toEqual([]);
    expect(snapshot.manifest.required_models).toEqual([]);
    expect(snapshot.manifest.oauth_scopes).toEqual({});
  });

  it("parses a manifest declaring OAuth scopes (#241)", () => {
    const snapshot = ModuleSnapshot.parse({
      manifest: {
        name: "calendar",
        version: "0.6.0",
        oauth_scopes: { google: ["https://www.googleapis.com/auth/calendar"] },
      },
      status: { healthy: true },
    });
    expect(snapshot.manifest.oauth_scopes.google).toEqual([
      "https://www.googleapis.com/auth/calendar",
    ]);
  });

  it("parses a manifest declaring model slots (#128)", () => {
    const snapshot = ModuleSnapshot.parse({
      manifest: {
        name: "knowledge",
        version: "0.6.0",
        required_models: [{ key: "embedding", role: "embedding", label: "Embedding model" }],
      },
      status: { healthy: true },
    });
    expect(snapshot.manifest.required_models[0].key).toBe("embedding");
    expect(snapshot.manifest.required_models[0].role).toBe("embedding");
  });

  it("defaults a module snapshot to enabled with no tags, and round-trips both (#126)", () => {
    const dflt = ModuleSnapshot.parse({
      manifest: { name: "m", version: "1.0" },
      status: { healthy: true },
    });
    expect(dflt.enabled).toBe(true);
    expect(dflt.manifest.tags).toEqual([]);

    const set = ModuleSnapshot.parse({
      manifest: { name: "m", version: "1.0", tags: ["calendar", "google"] },
      status: { healthy: true },
      enabled: false,
    });
    expect(set.enabled).toBe(false);
    expect(set.manifest.tags).toEqual(["calendar", "google"]);
  });

  it("parses a module page spec with archetype defaults (ADR-0018)", () => {
    const page = PageSpec.parse({ id: "files", title: "Files", archetype: "browser" });
    expect(page.icon).toBe("puzzle");
    expect(page.nav_order).toBe(100);
  });

  it("rejects an unknown page archetype", () => {
    expect(() => PageSpec.parse({ id: "x", title: "X", archetype: "kanban" })).toThrow();
  });

  it("parses a manifest carrying module pages", () => {
    const snapshot = ModuleSnapshot.parse({
      manifest: {
        name: "files",
        version: "0.1.0",
        pages: [{ id: "browse", title: "Files", archetype: "browser", icon: "folder", nav_order: 5 }],
      },
      status: { healthy: true },
    });
    expect(snapshot.manifest.pages[0].archetype).toBe("browser");
    expect(snapshot.manifest.pages[0].nav_order).toBe(5);
  });

  it("parses the browser archetype data shape", () => {
    const data = BrowserData.parse({
      title: "Echoes",
      items: [{ id: "a", title: "a", subtitle: "s", body: "b" }],
    });
    expect(data.items[0].body).toBe("b");
  });

  it("parses the board archetype data shape with tool-backed actions (ADR-0018)", () => {
    const data = BoardData.parse({
      title: "Tasks",
      columns: [
        {
          id: "today",
          title: "Today",
          cards: [
            {
              id: "t1",
              title: "Buy milk",
              subtitle: "2 litres",
              badges: [{ label: "2026-06-14", tone: "accent" }],
              actions: [{ tool: "tasks_complete", label: "Complete", args: { task_id: "t1" } }],
            },
          ],
        },
      ],
      actions: [{ tool: "tasks_add", label: "Add task", intent: "primary", form: true }],
    });
    expect(data.columns[0].cards[0].actions[0].tool).toBe("tasks_complete");
    expect(data.columns[0].cards[0].actions[0].args).toEqual({ task_id: "t1" });
    expect(data.actions[0].intent).toBe("primary");
    // unspecified knobs take their defaults
    expect(data.columns[0].cards[0].done).toBe(false);
    expect(data.actions[0].args).toEqual({});
  });

  it("defaults a board badge tone to dim", () => {
    const data = BoardData.parse({
      columns: [{ id: "c", title: "C", cards: [{ id: "x", title: "x", badges: [{ label: "due" }] }] }],
    });
    expect(data.columns[0].cards[0].badges[0].tone).toBe("dim");
  });

  it("rejects a danger board action without a confirm prompt (mirrors UiAction)", () => {
    expect(() =>
      BoardData.parse({ columns: [], actions: [{ tool: "rm", label: "Delete", intent: "danger" }] }),
    ).toThrow();
  });

  it("accepts a danger board action that carries a confirm prompt", () => {
    const data = BoardData.parse({
      columns: [],
      actions: [{ tool: "rm", label: "Delete", intent: "danger", confirm: "Delete it?" }],
    });
    expect(data.actions[0].confirm).toBe("Delete it?");
  });

  it("parses entity references on a message and a turn (ADR-0019)", () => {
    const rec = MessageRecord.parse({
      role: "assistant",
      content: "see your standup",
      created_at: "2026-06-14T09:00:00Z",
      entity_refs: [{ ref_id: "e1", module: "calendar", kind: "event", title: "Standup" }],
    });
    expect(rec.entity_refs[0].title).toBe("Standup");

    const turn = AgentTurn.parse({
      content: "ok",
      tools_used: [],
      stopped: "completed",
      entity_refs: [{ ref_id: "e1", module: "m", kind: "k", title: "T" }],
    });
    expect(turn.entity_refs[0].ref_id).toBe("e1");
  });

  it("defaults message entity_refs + attachments to empty (older transcripts stay valid)", () => {
    const rec = MessageRecord.parse({
      role: "user",
      content: "hi",
      created_at: "2026-06-14T09:00:00Z",
    });
    expect(rec.entity_refs).toEqual([]);
    expect(rec.attachments).toEqual([]);
  });

  it("parses message attachments (ADR-0019)", () => {
    const rec = MessageRecord.parse({
      role: "user",
      content: "summarize these",
      created_at: "2026-06-14T09:00:00Z",
      attachments: [
        { att_id: "a1", source: "file", kind: "text/plain", title: "notes.txt" },
        { att_id: "a2", source: "chat", ref_id: "s9", title: "earlier chat" },
      ],
    });
    expect(rec.attachments[0].source).toBe("file");
    expect(rec.attachments[1].ref_id).toBe("s9");
  });

  it("rejects an unknown attachment source", () => {
    expect(() => Attachment.parse({ att_id: "a1", source: "magic" })).toThrow();
  });

  it("parses the calendar archetype data, coercing timestamps to Date (ADR-0018)", () => {
    const data = CalendarData.parse({
      provider: "local",
      range: { start: "2026-06-01T00:00:00Z", end: "2026-07-01T00:00:00Z" },
      events: [
        {
          id: "e1",
          title: "Standup",
          start: "2026-06-15T09:00:00Z",
          end: "2026-06-15T09:30:00Z",
          location: "Room 4",
        },
      ],
    });
    expect(data.events[0].start instanceof Date).toBe(true);
    expect(data.events[0].title).toBe("Standup");
    expect(data.range?.start instanceof Date).toBe(true);
  });

  it("defaults calendar events to empty (a quiet calendar stays valid)", () => {
    expect(CalendarData.parse({}).events).toEqual([]);
  });

  it("parses an all-day event as a floating local date (no one-day-early shift)", () => {
    // The module sends an all-day endpoint as a bare date; it must land on that calendar
    // day in every timezone — never the day before (the bug a UTC instant caused).
    const ev = CalendarEvent.parse({
      id: "e1",
      title: "Holiday",
      start: "2026-06-15",
      end: "2026-06-16",
      all_day: true,
    });
    expect(ev.all_day).toBe(true);
    expect(ev.start.getFullYear()).toBe(2026);
    expect(ev.start.getMonth()).toBe(5); // June (0-based) — not shifted to May/14th
    expect(ev.start.getDate()).toBe(15);
    expect(ev.start.getHours()).toBe(0); // local midnight, not a UTC instant
  });

  it("defaults a calendar event's recurrence/attendees to empty (#432)", () => {
    const ev = CalendarEvent.parse({
      id: "e1",
      title: "One-off",
      start: "2026-06-15T09:00:00Z",
      end: "2026-06-15T09:30:00Z",
    });
    expect(ev.recurrence).toBeFalsy();
    expect(ev.recurring_event_id).toBeFalsy();
    expect(ev.attendees).toEqual([]);
  });

  it("parses a recurring event's rule, series id, and guest list (#432)", () => {
    const ev = CalendarEvent.parse({
      id: "s1_20260622T090000Z",
      title: "Standup",
      start: "2026-06-22T09:00:00Z",
      end: "2026-06-22T09:30:00Z",
      recurring_event_id: "s1",
      attendees: [
        { email: "alice@example.com", response_status: "accepted" },
        { email: "bob@example.com" },
      ],
    });
    expect(ev.recurring_event_id).toBe("s1");
    expect(ev.attendees).toHaveLength(2);
    expect(ev.attendees[0].response_status).toBe("accepted");
    expect(ev.attendees[1].response_status).toBe("needsAction"); // default when omitted
  });

  it("parseEventDate keeps a timed instant but floats an all-day date", () => {
    // Timed: a real instant (UTC here) — read in local zone like any calendar.
    expect(parseEventDate("2026-06-15T09:00:00Z", false).getTime()).toBe(
      new Date("2026-06-15T09:00:00Z").getTime(),
    );
    // All-day: built from the local date parts, so the calendar day never shifts.
    const allDay = parseEventDate("2026-06-15", true);
    expect(allDay.getDate()).toBe(15);
    expect(allDay.getMonth()).toBe(5);
  });

  it("parses the model catalog snapshot, coercing updated_at to a Date (#269)", () => {
    const snap = CatalogResponse.parse({
      source: "https://ollama.com/library",
      updated_at: "2026-06-23T12:00:00Z",
      stale: false,
      entries: [
        {
          id: "llama3.1:8b",
          family: "llama3.1",
          params: "8b",
          description: "A general assistant.",
          tags: ["general"],
          pulls: "116.3M",
        },
      ],
    });
    expect(snap.updated_at instanceof Date).toBe(true);
    expect(snap.entries[0].id).toBe("llama3.1:8b");
    expect(snap.entries[0].size_gb ?? null).toBeNull(); // omitted upstream
  });

  it("defaults catalog entry fields and accepts a seeded/stale snapshot", () => {
    const snap = CatalogResponse.parse({
      source: "https://ollama.com/library",
      updated_at: null,
      stale: true,
      entries: [{ id: "nomic-embed-text", family: "nomic-embed-text" }],
    });
    expect(snap.stale).toBe(true);
    expect(snap.updated_at).toBeNull();
    // params/description/tags fall back to their defaults for a sparse entry.
    expect(snap.entries[0].params).toBe("");
    expect(snap.entries[0].tags).toEqual([]);
  });

  it("parses LLM prefs including the context-window setting", () => {
    const prefs = LlmPrefs.parse({
      global_default: "llama3.2",
      global_embed_default: null,
      global_context_window: 16384,
      kv_cache_type: null,
      global_agent_max_steps: 6,
      hidden: [],
    });
    expect(prefs.global_context_window).toBe(16384);
    expect(prefs.global_agent_max_steps).toBe(6);
    // null = follow the env/runtime default
    const unset = LlmPrefs.parse({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    });
    expect(unset.global_context_window).toBeNull();
  });

  it("parses a system-info snapshot with a GPU and a suggestion", () => {
    const info = SystemInfo.parse({
      gpu: { vendor: "nvidia", name: "RTX 4090", vram_total_mb: 24564, vram_free_mb: 23000 },
      ram_total_mb: 32000,
      model: { name: "llama3.2:latest", size_mb: 4482 },
      suggested_context: { min: 2048, suggested: 16384, max: 24000 },
    });
    expect(info.gpu?.vendor).toBe("nvidia");
    expect(info.suggested_context?.suggested).toBe(16384);
    expect(info.model?.size_mb).toBe(4482);
  });

  it("parses a system-info snapshot with no GPU (CPU fallback)", () => {
    const info = SystemInfo.parse({ gpu: null, ram_total_mb: 16000, model: null });
    expect(info.gpu).toBeNull();
    expect(info.ram_total_mb).toBe(16000);
    expect(info.suggested_context ?? null).toBeNull();
  });
});
