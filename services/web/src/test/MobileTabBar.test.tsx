import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

// The service-worker virtual module is a Vite plugin shim — absent under vitest.
vi.mock("virtual:pwa-register/react", () => ({
  useRegisterSW: () => ({ needRefresh: [false], updateServiceWorker: vi.fn() }),
}));

// MobileTabBar's Notifications entry polls the unread count (#671) via useQuery.
vi.mock("@/lib/api", () => ({
  api: { notificationsUnreadCount: vi.fn().mockResolvedValue({ count: 0 }) },
}));

import { MobileTabBar } from "@/App";
import type { ModulePageNav } from "@/app/registry";

// The phone tab bar overflows once module pages join (#480): gradient edge fades must
// signal the hidden side(s). jsdom has no layout, so scroll metrics are modelled.

const pages: ModulePageNav[] = [
  { path: "/m/calendar/calendar", module: "calendar", pageId: "calendar", label: "Calendar", archetype: "calendar", icon: "calendar", navOrder: 20 },
  { path: "/m/tasks/board", module: "tasks", pageId: "board", label: "Tasks", archetype: "board", icon: "check", navOrder: 30 },
];

const fades = (container: HTMLElement) =>
  [...container.querySelectorAll("[data-fade]")].map((el) => el.getAttribute("data-fade"));

function renderBar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MobileTabBar modulePages={pages} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const scroller = utils.container.querySelector(".overflow-x-auto") as HTMLElement;
  Object.defineProperty(scroller, "scrollWidth", { value: 700, configurable: true });
  Object.defineProperty(scroller, "clientWidth", { value: 375, configurable: true });
  return { ...utils, scroller };
}

describe("MobileTabBar overflow affordance (#480)", () => {
  it("renders every destination, core surfaces first", () => {
    renderBar();
    expect(screen.getByText("Chat")).toBeInTheDocument();
    expect(screen.getByText("Calendar")).toBeInTheDocument();
    expect(screen.getByText("Tasks")).toBeInTheDocument();
  });

  it("fades only the far edge at rest, both mid-scroll, only the near edge at the end", () => {
    const { container, scroller } = renderBar();

    scroller.scrollLeft = 0;
    fireEvent.scroll(scroller);
    expect(fades(container)).toEqual(["right"]);

    scroller.scrollLeft = 150;
    fireEvent.scroll(scroller);
    expect(fades(container)).toEqual(["left", "right"]);

    scroller.scrollLeft = 325; // 700 - 375 = fully scrolled
    fireEvent.scroll(scroller);
    expect(fades(container)).toEqual(["left"]);
  });

  it("shows no fades when everything fits", () => {
    const { container, scroller } = renderBar();
    Object.defineProperty(scroller, "scrollWidth", { value: 375, configurable: true });
    scroller.scrollLeft = 0;
    fireEvent.scroll(scroller);
    expect(fades(container)).toEqual([]);
  });
});
