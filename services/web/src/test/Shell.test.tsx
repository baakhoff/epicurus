import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { Shell } from "@/App";

// The service-worker virtual module is a Vite plugin shim — absent under vitest.
vi.mock("virtual:pwa-register/react", () => ({
  useRegisterSW: () => ({ needRefresh: [false], updateServiceWorker: vi.fn() }),
}));

const mockUnreadCount = vi.fn();
// The layout is the unit under test; keep the data plane quiet and deterministic.
vi.mock("@/lib/api", () => ({
  api: {
    modules: vi.fn().mockResolvedValue([]),
    power: vi.fn().mockResolvedValue({ state: "idle" }),
    setPower: vi.fn().mockResolvedValue({ state: "idle" }),
    notificationsUnreadCount: (...a: unknown[]) => mockUnreadCount(...a),
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
// interface. The fixed-height shell let overflow escape to <body>, and the rail had no
// scroll region of its own — so the wheel event chained to the body. The shell must clip
// itself (`overflow-hidden`) and the rail must scroll its own links.
//
// The shell fills the fixed #root with `h-full` rather than re-measuring the viewport
// with `h-dvh`: on a phone the latter disagreed with the root while the address bar was
// showing and clipped the bottom tab bar out of view after a refresh.
describe("Shell scroll containment", () => {
  it("clips the fixed-height shell so the body never scrolls", () => {
    const { container } = renderShell();
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("h-full");
    expect(root.className).toContain("overflow-hidden");
  });

  it("gives the side rail its own vertical scroll", () => {
    renderShell();
    const rail = screen.getByRole("navigation", { name: "Primary" });
    expect(rail.className).toContain("overflow-y-auto");
  });
});

// The Notifications nav entry is the one surface with a live unread badge (#671) — every
// other Surface stays plain.
describe("Notifications unread badge", () => {
  it("shows no badge when there are no unread notifications", async () => {
    mockUnreadCount.mockReset().mockResolvedValue({ count: 0 });
    renderShell();
    const rail = await screen.findByRole("navigation", { name: "Primary" });
    const link = within(rail).getByRole("link", { name: /notifications/i });
    await waitFor(() => expect(mockUnreadCount).toHaveBeenCalled());
    expect(within(link).queryByText(/^\d+$/)).not.toBeInTheDocument();
  });

  it("shows the unread count on the Notifications nav entry", async () => {
    mockUnreadCount.mockReset().mockResolvedValue({ count: 3 });
    renderShell();
    const rail = await screen.findByRole("navigation", { name: "Primary" });
    const link = within(rail).getByRole("link", { name: /notifications/i });
    expect(await within(link).findByText("3")).toBeInTheDocument();
  });

  it("caps the displayed count at 99+", async () => {
    mockUnreadCount.mockReset().mockResolvedValue({ count: 150 });
    renderShell();
    const rail = await screen.findByRole("navigation", { name: "Primary" });
    const link = within(rail).getByRole("link", { name: /notifications/i });
    expect(await within(link).findByText("99+")).toBeInTheDocument();
  });
});
