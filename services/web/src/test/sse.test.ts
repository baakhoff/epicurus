import { describe, expect, it } from "vitest";

import { parseFrame } from "@/lib/sse";

describe("parseFrame", () => {
  it("parses an event + data frame", () => {
    expect(parseFrame('event: delta\ndata: {"text":"hi"}')).toEqual({
      event: "delta",
      data: '{"text":"hi"}',
    });
  });

  it("defaults the event name to message", () => {
    expect(parseFrame("data: x")).toEqual({ event: "message", data: "x" });
  });

  it("joins multi-line data", () => {
    expect(parseFrame("data: a\ndata: b")).toEqual({ event: "message", data: "a\nb" });
  });

  it("ignores comments (heartbeats) and yields nothing without data", () => {
    expect(parseFrame(": ping")).toBeNull();
    expect(parseFrame("event: delta")).toBeNull();
  });
});
