/**
 * Tests for `runStream` — the Automation runs feed's transport (#669).
 *
 * The scripted-async-generator pattern from eventStream.test.ts: `importOriginal` +
 * spread keeps `parseFrame` real and replaces only the wire, so what is exercised is the
 * frame filtering and the zod parse, which is where this function's behavior lives.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AutomationRun } from "@/lib/contracts";
import type { SseMessage } from "@/lib/sse";

const requested: { path: string; init: unknown }[] = [];
let scripted: SseMessage[] = [];

vi.mock("@/lib/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/sse")>();
  return {
    ...actual,
    sseRequest: async function* (path: string, init: unknown): AsyncGenerator<SseMessage> {
      requested.push({ path, init });
      for (const message of scripted) yield message;
    },
  };
});

import { runStream } from "@/lib/api";

function frame(overrides: Record<string, unknown> = {}): SseMessage {
  return {
    event: "automation_run",
    data: JSON.stringify({
      id: "r1",
      automation_id: "a1",
      started_at: "2026-07-20T09:00:00Z",
      trigger_refs: [42],
      filter_verdict: "matched",
      model: "qwen2.5:7b",
      prompt_tokens: 812,
      completion_tokens: 96,
      duration_ms: 4210,
      outcome: "ok",
      error: null,
      output: "done",
      sinks_fired: ["chat"],
      trigger_entity_refs: [],
      ...overrides,
    }),
  };
}

async function collect(stream: AsyncGenerator<AutomationRun> = runStream()) {
  const out: AutomationRun[] = [];
  for await (const run of stream) out.push(run);
  return out;
}

beforeEach(() => {
  requested.length = 0;
  scripted = [];
});

describe("runStream", () => {
  it("parses automation_run frames into typed runs", async () => {
    scripted = [frame()];
    const runs = await collect();
    expect(runs).toHaveLength(1);
    expect(runs[0].automation_id).toBe("a1");
    expect(runs[0].trigger_refs).toEqual([42]);
    expect(runs[0].sinks_fired).toEqual(["chat"]);
  });

  it("requests the plain stream when no filters are given", async () => {
    await collect();
    expect(requested[0].path).toBe("/platform/v1/automations/runs/stream");
  });

  it("threads the automation and outcome filters into the query", async () => {
    await collect(runStream("a1", "skipped"));
    expect(requested[0].path).toBe(
      "/platform/v1/automations/runs/stream?automation_id=a1&outcome=skipped",
    );
  });

  it("ignores frames of other event kinds", async () => {
    scripted = [{ event: "noise", data: "{}" }, frame()];
    expect(await collect()).toHaveLength(1);
  });

  it("skips a malformed frame rather than dying mid-stream", async () => {
    scripted = [{ event: "automation_run", data: "{not json" }, frame({ id: "r2" })];
    const runs = await collect();
    expect(runs.map((r) => r.id)).toEqual(["r2"]);
  });

  it("parses a skipped run whose why rides the error field", async () => {
    scripted = [
      frame({
        outcome: "skipped",
        error: "rate cap reached (4/hour)",
        model: null,
        prompt_tokens: null,
        completion_tokens: null,
        output: "",
        sinks_fired: [],
      }),
    ];
    const [run] = await collect();
    expect(run.outcome).toBe("skipped");
    expect(run.error).toBe("rate cap reached (4/hour)");
  });
});
