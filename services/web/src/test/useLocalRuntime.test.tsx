/**
 * The one query every surface reads the local-runtime state from (#962).
 *
 * Two of these are guards against silent rot rather than feature tests: an unknown answer must
 * read as `ok` (an older core 404s, and hiding the local half of the Models page from a
 * deployment that *has* a runtime is the worse failure), and `settled` must latch — the
 * components it gates subscribe to this same query, so a `settled` that can flip back to false
 * makes the page oscillate forever.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useLocalRuntime } from "@/lib/useLocalRuntime";

const mockLocalRuntime = vi.fn();
vi.mock("@/lib/api", () => ({ api: { localRuntime: () => mockLocalRuntime() } }));

function Probe() {
  const runtime = useLocalRuntime();
  return (
    <div data-testid="runtime">
      {runtime.state}|{String(runtime.absent)}|{String(runtime.unreachable)}|
      {String(runtime.settled)}
    </div>
  );
}

/** A second subscriber mounted only once the first has settled — exactly the shape the Models
 *  page has, and the one that used to send an errored query round and round. */
function GatedProbes() {
  const runtime = useLocalRuntime();
  return (
    <>
      <Probe />
      {runtime.settled && <Probe />}
    </>
  );
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const readout = () => screen.getAllByTestId("runtime")[0].textContent;

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useLocalRuntime", () => {
  it("reports `absent` as absent, and not as an error", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "absent", url_configured: false });
    render(<Probe />, { wrapper });

    await waitFor(() => expect(readout()).toBe("absent|true|false|true"));
  });

  it("reports `unreachable` as the error it is", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "unreachable", url_configured: true });
    render(<Probe />, { wrapper });

    await waitFor(() => expect(readout()).toBe("unreachable|false|true|true"));
  });

  it("reports `ok` for a healthy runtime", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "ok", url_configured: true });
    render(<Probe />, { wrapper });

    await waitFor(() => expect(readout()).toBe("ok|false|false|true"));
  });

  it("falls back to `ok` when the endpoint is missing (an older core)", async () => {
    mockLocalRuntime.mockRejectedValue(new Error("404 Not Found"));
    render(<Probe />, { wrapper });

    await waitFor(() => expect(readout()).toBe("ok|false|false|true"));
  });

  it("keeps `settled` latched while a gated subscriber mounts and refetches", async () => {
    // An errored query is permanently stale, so mounting a second subscriber refetches it. If
    // `settled` tracked "is a fetch in flight", that subscriber would unmount itself again on
    // every loop. Assert it stays settled through several rounds of exactly that.
    mockLocalRuntime.mockRejectedValue(new Error("404 Not Found"));
    render(<GatedProbes />, { wrapper });

    await waitFor(() => expect(screen.getAllByTestId("runtime")).toHaveLength(2));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getAllByTestId("runtime")).toHaveLength(2);
    expect(readout()).toBe("ok|false|false|true");
  });
});
