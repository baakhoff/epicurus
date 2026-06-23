import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { Shell } from "@/App";

// The service-worker virtual module is a Vite plugin shim — absent under vitest.
vi.mock("virtual:pwa-register/react", () => ({
  useRegisterSW: () => ({ needRefresh: [false], updateServiceWorker: vi.fn() }),
}));

// The layout is the unit under test; keep the data plane quiet and deterministic.
vi.mock("@/lib/api", () => ({
  api: {
    modules: vi.fn().mockResolvedValue([]),
    power: vi.fn().mockResolvedValue({ state: "idle" }),
    setPower: vi.fn().mockResolvedValue({ state: "idle" }),
  },
  logStream: vi.fn(),
}));

vi.mock("@/stores/downloads", () => ({
  useDownloads: (selector: (s: unknown) => unknown) =>
    selector({ active: {}, pull: vi.fn(), dismiss: vi.fn() }),
}));

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // A route with no matching module resolves to a tiny "no such page" notice, so the
  // shell renders without dragging a heavy screen (Chat, Models…) into the test.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/m/none/none"]}>
        <Shell />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// Regression guard: scrolling while hovering the side rail used to scroll the whole
// interface. The fixed-height (`h-dvh`) shell let overflow escape to <body>, and the
// rail had no scroll region of its own — so the wheel event chained to the body. The
// shell must clip itself and the rail must scroll its own links.
describe("Shell scroll containment", () => {
  it("clips the fixed-height shell so the body never scrolls", () => {
    const { container } = renderShell();
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("h-dvh");
    expect(root.className).toContain("overflow-hidden");
  });

  it("gives the side rail its own vertical scroll", () => {
    renderShell();
    const rail = screen.getByRole("navigation", { name: "Primary" });
    expect(rail.className).toContain("overflow-y-auto");
  });
});
