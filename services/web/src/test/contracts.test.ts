import { describe, expect, it } from "vitest";

import { AgentEvent, ModuleSnapshot } from "@/lib/contracts";

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
  });
});
