import { describe, expect, it } from "vitest";

import { readinessProgress, readinessSummary, toolLabel } from "@/lib/activity";
import type { Readiness } from "@/lib/contracts";

describe("toolLabel", () => {
  it("phrases known actions naturally across name styles", () => {
    expect(toolLabel("knowledge_search")).toBe("Searching knowledge");
    expect(toolLabel("calendar.list_events")).toBe("Reading calendar");
    expect(toolLabel("tasks_add")).toBe("Adding to tasks");
    expect(toolLabel("mail_send")).toBe("Sending mail");
  });

  it("falls back to a clean 'Calling …' for unknown actions", () => {
    expect(toolLabel("echo")).toBe("Calling echo");
    expect(toolLabel("weather.forecast")).toBe("Calling weather forecast");
  });
});

function _readiness(over: Partial<Readiness>): Readiness {
  return { ready: false, power: "idle", components: [], ...over };
}

describe("readinessSummary", () => {
  it("names the components still warming", () => {
    const r = _readiness({
      components: [
        { name: "modules", ready: true, detail: "2/2 healthy" },
        { name: "model", ready: false, detail: "warming" },
      ],
    });
    expect(readinessSummary(r)).toBe("Warming the model");
  });

  it("reports ready and asleep states", () => {
    expect(readinessSummary(_readiness({ ready: true, components: [] }))).toBe("Ready");
    expect(readinessSummary(_readiness({ power: "paused" }))).toContain("Asleep");
  });
});

describe("readinessProgress", () => {
  it("is full when ready and a visible sliver when nothing is ready yet", () => {
    expect(readinessProgress(_readiness({ ready: true }))).toBe(1);
    expect(
      readinessProgress(
        _readiness({ components: [{ name: "model", ready: false, detail: "checking…" }] }),
      ),
    ).toBeCloseTo(0.15);
  });

  it("reflects the share of ready components", () => {
    const r = _readiness({
      components: [
        { name: "modules", ready: true, detail: "ok" },
        { name: "model", ready: false, detail: "warming" },
      ],
    });
    expect(readinessProgress(r)).toBeCloseTo(0.5);
  });
});
