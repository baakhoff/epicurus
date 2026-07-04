import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Shell } from "@/App";
import { useConnection } from "@/stores/connection";
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

beforeEach(() => {
  useConnection.setState({ online: true, coreDown: false });
  useToasts.setState({ toasts: [] });
  useDownloads.setState({ active: {} });
});

// The shell-level connection banner (#494): a cached PWA shell renders fine while
// nothing behind it is reachable — the banner names which of the two silences it is,
// and clears itself the moment evidence recovers.
describe("ConnectionBanner (#494)", () => {
  it("stays silent while everything is healthy", () => {
    renderShell();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("names the dead-core state while the device itself is online", () => {
    renderShell();
    act(() => useConnection.getState().reportUnreachable());
    expect(screen.getByRole("status")).toHaveTextContent("can't reach epicurus — retrying");
  });

  it("prefers the offline wording when the device has no network at all", () => {
    renderShell();
    // Both signals firing (offline phones also fail their probes) must read as offline —
    // that distinction is the issue's "dead core vs true offline" requirement.
    act(() => {
      useConnection.getState().setOnline(false);
      useConnection.getState().reportUnreachable();
    });
    expect(screen.getByRole("status")).toHaveTextContent("offline — reconnecting");
  });

  it("dismisses itself on recovery, with no reload", () => {
    renderShell();
    act(() => useConnection.getState().reportUnreachable());
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => useConnection.getState().reportReachable());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
