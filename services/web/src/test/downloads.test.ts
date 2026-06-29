import { beforeEach, describe, expect, it, vi } from "vitest";

// ── mock the SSE transport and the API client ─────────────────────────────────
// The store consumes `sse(...)` and, on completion, asks the core for a per-model context (#386).

const mockSse = vi.fn();
vi.mock("@/lib/sse", () => ({ sse: (...args: unknown[]) => mockSse(...args) }));

const mockSuggest = vi.fn();
vi.mock("@/lib/api", () => ({ api: { suggestModelContext: (m: string) => mockSuggest(m) } }));

import { useDownloads } from "@/stores/downloads";

/** An async generator that replays a fixed list of SSE frames, like the real stream. */
async function* frames(messages: { event: string; data: string }[]) {
  for (const m of messages) yield m;
}

const DONE = [{ event: "done", data: '{"status":"ok"}' }];

beforeEach(() => {
  useDownloads.setState({ active: {} });
  mockSse.mockReset();
  mockSuggest.mockReset();
  // Always a promise — the store attaches `.catch`, so a non-promise would throw in the loop.
  mockSuggest.mockResolvedValue({ model: "x", context_window: 8192, applied: true });
});

describe("useDownloads.pull", () => {
  it("marks the download done and runs onFinished on the done event", async () => {
    mockSse.mockReturnValue(frames(DONE));
    const onFinished = vi.fn();

    await useDownloads.getState().pull("llama3.2:3b", onFinished);

    expect(onFinished).toHaveBeenCalledOnce();
    expect(useDownloads.getState().active["llama3.2:3b"].done).toBe(true);
  });

  it("asks the core for a recommended per-model context when a pull finishes (#386)", async () => {
    mockSse.mockReturnValue(frames(DONE));

    await useDownloads.getState().pull("llama3.2:3b", () => {});

    expect(mockSuggest).toHaveBeenCalledWith("llama3.2:3b");
  });

  it("does not request a context suggestion until the pull is actually done (#386)", async () => {
    mockSse.mockReturnValue(
      frames([{ event: "progress", data: '{"status":"pulling","total":100,"completed":50}' }]),
    );

    await useDownloads.getState().pull("m", () => {});

    expect(mockSuggest).not.toHaveBeenCalled();
    expect(useDownloads.getState().active["m"].done).toBe(false);
  });

  it("swallows a suggest-context failure — the finished download is unaffected (#386)", async () => {
    mockSse.mockReturnValue(frames(DONE));
    mockSuggest.mockRejectedValue(new Error("boom"));
    const onFinished = vi.fn();

    await expect(useDownloads.getState().pull("m", onFinished)).resolves.toBeUndefined();

    expect(onFinished).toHaveBeenCalledOnce();
    const dl = useDownloads.getState().active["m"];
    expect(dl.done).toBe(true);
    expect(dl.error).toBeNull();
  });

  it("records the error and never suggests a context on a failed pull", async () => {
    mockSse.mockReturnValue(frames([{ event: "error", data: '{"detail":"nope"}' }]));

    await useDownloads.getState().pull("m", () => {});

    const dl = useDownloads.getState().active["m"];
    expect(dl.error).toBe("nope");
    expect(dl.done).toBe(true);
    expect(mockSuggest).not.toHaveBeenCalled();
  });
});
