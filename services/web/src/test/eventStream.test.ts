/**
 * Tests for `eventStream` — the Events feed's transport.
 *
 * The scripted-async-generator pattern from chat.test.ts: `importOriginal` + spread keeps
 * `parseFrame` real and replaces only the wire, so what is exercised is the frame
 * filtering and the zod parse, which is where this function's behavior actually lives.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ModuleEvent } from "@/lib/contracts";
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

import { eventStream } from "@/lib/api";

function frame(overrides: Record<string, unknown> = {}): SseMessage {
  return {
    event: "module_event",
    data: JSON.stringify({
      id: 1,
      tenant: "local",
      module: "echo",
      type: "echo.pinged",
      occurred_at: "2026-07-17T12:00:00Z",
      received_at: "2026-07-17T12:00:01Z",
      dedup_key: "k1",
      entity_ref: null,
      payload: { note: "hi" },
      schema_version: 1,
      ...overrides,
    }),
  };
}

async function collect(stream: AsyncGenerator<ModuleEvent> = eventStream()) {
  const out: ModuleEvent[] = [];
  for await (const event of stream) out.push(event);
  return out;
}

beforeEach(() => {
  requested.length = 0;
  scripted = [];
});

describe("eventStream", () => {
  it("parses module_event frames into typed events", async () => {
    scripted = [frame()];
    const events = await collect();
    expect(events).toHaveLength(1);
    expect(events[0].module).toBe("echo");
    expect(events[0].type).toBe("echo.pinged");
    expect(events[0].payload).toEqual({ note: "hi" });
  });

  it("carries an entity_ref through so the row can render a chip", async () => {
    scripted = [
      frame({
        entity_ref: { ref_id: "k1", module: "echo", kind: "ping", title: "a ping" },
      }),
    ];
    const [event] = await collect();
    expect(event.entity_ref?.title).toBe("a ping");
    expect(event.entity_ref?.module).toBe("echo");
  });

  it("ignores frames that are not module_event", async () => {
    // The feed may carry keepalives or, later, a second frame type; anything unrecognised
    // must not become an event.
    scripted = [{ event: "ping", data: "{}" }, frame()];
    expect(await collect()).toHaveLength(1);
  });

  it("skips a malformed frame instead of killing the stream", async () => {
    // One bad frame must not end the feed — the rest of the tail still arrives.
    scripted = [
      { event: "module_event", data: "not json" },
      { event: "module_event", data: JSON.stringify({ id: "not a number" }) },
      frame({ id: 7 }),
    ];
    const events = await collect();
    expect(events).toHaveLength(1);
    expect(events[0].id).toBe(7);
  });

  it("streams over GET with no filters by default", async () => {
    scripted = [];
    await collect();
    expect(requested[0].path).toBe("/platform/v1/events/stream");
    expect(requested[0].init).toMatchObject({ method: "GET" });
  });

  it("passes module and type filters as query params", async () => {
    await collect(eventStream("mail", "mail.received"));
    expect(requested[0].path).toBe("/platform/v1/events/stream?module=mail&type=mail.received");
  });

  it("omits empty filters from the query", async () => {
    await collect(eventStream(undefined, "echo.pinged"));
    expect(requested[0].path).toBe("/platform/v1/events/stream?type=echo.pinged");
  });

  it("forwards the abort signal so the caller can stop the stream", async () => {
    const controller = new AbortController();
    await collect(eventStream(undefined, undefined, controller.signal));
    expect(requested[0].init).toMatchObject({ signal: controller.signal });
  });
});
