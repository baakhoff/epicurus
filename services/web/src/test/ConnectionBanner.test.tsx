import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Shell } from "@/App";
import { UNREACHABLE_GRACE_MS, useConnection, type UnreachableEvidence } from "@/stores/connection";
import { useDownloads } from "@/stores/downloads";
import { useToasts } from "@/stores/toasts";

vi.mock("virtual:pwa-register/react", () => ({
  useRegisterSW: () => ({ needRefresh: [false], updateServiceWorker: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    modules: vi.fn().mockResolvedValue([]),
    power: vi.fn().mockResolvedValue({ state: "idle" }),
    setPower: vi.fn().mockResolvedValue({ state: "idle" }),
  },
  logStream: vi.fn(),
}));

const EVIDENCE: UnreachableEvidence = {
  method: "GET",
  path: "/platform/v1/power",
  kind: "502",
};

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/m/none/none"]}>
        <Shell />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// The Shell mounts the real useConnectionWatch, so arming pendingDown fires the #791
// confirming re-probe (queryClient.refetchQueries against the mocked api.power/modules
// above) — an async act() flushes that microtask chain instead of leaving it dangling
// across the assertion below.
async function report(evidence: UnreachableEvidence) {
  await act(async () => {
    useConnection.getState().reportUnreachable(evidence);
  });
}

async function advance(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  useConnection.setState({
    online: true,
    coreDown: false,
    pendingDown: false,
    confirmedDown: false,
    lastEvidence: null,
  });
  useToasts.setState({ toasts: [] });
  useDownloads.setState({ active: {} });
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

// The shell-level connection banner (#494): a cached PWA shell renders fine while
// nothing behind it is reachable — the banner names which of the two silences it is,
// and clears itself the moment evidence recovers. Debounced since #791: a single blip
// must never show it, only sustained or twice-confirmed evidence may.
describe("ConnectionBanner (#494, debounced #791)", () => {
  it("stays silent while everything is healthy", () => {
    renderShell();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("stays silent through a single transient 502 that clears before the grace window", async () => {
    renderShell();
    await report(EVIDENCE);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    // In the real app, the confirming re-probe (useConnectionWatch → epFetch) resolving
    // healthy is what calls this — the api.* mock above bypasses epFetch entirely, so a
    // genuinely transient blip's recovery is driven directly here to model that outcome.
    await act(async () => useConnection.getState().reportReachable());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    // Past the original grace deadline now — proves the timer was cancelled outright,
    // not merely that confirmedDown hadn't ticked over yet.
    await advance(UNREACHABLE_GRACE_MS);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("stays silent through a single transient TypeError that clears before the grace window", async () => {
    renderShell();
    await report({ method: "POST", path: "/platform/v1/agent/send", kind: "TypeError" });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await act(async () => useConnection.getState().reportReachable());
    await advance(UNREACHABLE_GRACE_MS);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders once a failure persists past the grace window", async () => {
    renderShell();
    // Nothing here ever confirms the mocked re-probe recovered or failed again, so
    // pendingDown stays armed on its own until the grace timer itself fires.
    await report(EVIDENCE);
    await advance(UNREACHABLE_GRACE_MS - 1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await advance(1);
    expect(screen.getByRole("status")).toHaveTextContent("can't reach epicurus — retrying");
  });

  it("renders immediately on a second confirming failure, without waiting out the grace", async () => {
    renderShell();
    await report(EVIDENCE);
    await advance(500); // well under the grace window
    await report({ method: "GET", path: "/platform/v1/modules", kind: "504" });
    expect(screen.getByRole("status")).toHaveTextContent("can't reach epicurus — retrying");
  });

  it("names the dead-core state while the device itself is online", async () => {
    renderShell();
    await report(EVIDENCE);
    await advance(UNREACHABLE_GRACE_MS);
    expect(screen.getByRole("status")).toHaveTextContent("can't reach epicurus — retrying");
  });

  it("prefers the offline wording when the device has no network at all", () => {
    renderShell();
    // Both signals firing (offline phones also fail their probes) must read as offline —
    // that distinction is the issue's "dead core vs true offline" requirement. offline
    // wins outright, with no debounce — navigator.onLine is not flaky the way a single
    // dropped fetch is.
    act(() => {
      useConnection.getState().setOnline(false);
    });
    expect(screen.getByRole("status")).toHaveTextContent("offline — reconnecting");
  });

  it("clears on the first healthy response after confirming, with no reload", async () => {
    renderShell();
    await report(EVIDENCE);
    await advance(UNREACHABLE_GRACE_MS);
    expect(screen.getByRole("status")).toBeInTheDocument();
    await act(async () => useConnection.getState().reportReachable());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("names the last failing request and failure class in the tooltip", async () => {
    renderShell();
    await report({ method: "POST", path: "/platform/v1/agent/attachments", kind: "504" });
    await advance(UNREACHABLE_GRACE_MS);
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      "POST /platform/v1/agent/attachments — 504",
    );
  });

  it("updates the tooltip to the freshest evidence while still confirmed", async () => {
    renderShell();
    await report(EVIDENCE);
    await advance(UNREACHABLE_GRACE_MS);
    expect(screen.getByRole("status")).toHaveAttribute("title", "GET /platform/v1/power — 502");

    await report({ method: "GET", path: "/platform/v1/modules", kind: "TypeError" });
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      "GET /platform/v1/modules — TypeError",
    );
  });
});
