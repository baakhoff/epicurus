import { describe, expect, it } from "vitest";

import { AgentEvent, BrowserData, ModuleSnapshot, PageSpec } from "@/lib/contracts";

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
});
