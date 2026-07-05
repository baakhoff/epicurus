import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Shell } from "@/App";
import { useDownloads } from "@/stores/downloads";
import { toast, useToasts } from "@/stores/toasts";

// An update is pending for the whole file, so the UpdateToast is always up.
vi.mock("virtual:pwa-register/react", () => ({
  useRegisterSW: () => ({ needRefresh: [true], updateServiceWorker: vi.fn() }),
}));

// The corner region is the unit under test; keep the data plane quiet.
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
  // A no-module route keeps the heavy screens (Chat, Models…) out of the render.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/m/none/none"]}>
        <Shell />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useToasts.setState({ toasts: [] });
  useDownloads.setState({ active: {} });
});

// The corner-anchored surfaces used to pin the same fixed corner independently (#510):
// Toaster z-70, UpdateToast z-50, DownloadTray z-40 — a toast firing while the update
// banner showed simply covered it (higher z wins; nothing offsets). All three now render
// as flow children of ONE positioned CornerStack column, so they stack instead.
describe("CornerStack (#510)", () => {
  it("shows a toast, the update prompt, and a download pill simultaneously in one column", () => {
    useDownloads.setState({
      active: {
        "llama3:8b": {
          model: "llama3:8b",
          status: "downloading",
          total: 100,
          completed: 42,
          error: null,
          done: false,
        },
      },
    });
    renderShell();
    act(() => toast.error("Could not save."));

    const updateCard = screen.getByText("A new epicurus is ready.").parentElement;
    const toastCard = screen.getByRole("status");
    const trayRow = screen.getByRole("button", { name: /llama3:8b/ }).parentElement;
    if (!updateCard || !trayRow) throw new Error("corner surfaces not rendered");

    // One shared positioned column — the region is fixed, the surfaces just flow in it.
    const stack = updateCard.parentElement;
    if (!stack) throw new Error("update card rendered outside the corner stack");
    expect(toastCard.parentElement).toBe(stack);
    expect(trayRow.parentElement).toBe(stack);
    expect(stack.className).toContain("fixed");
    expect(stack.className).toContain("flex-col");
    expect(stack.className).toContain("pointer-events-none");

    // No surface re-introduces its own fixed pinning — that IS the occlusion mechanism.
    for (const el of [updateCard, toastCard, trayRow]) {
      expect(el.className).not.toContain("fixed");
    }
  });

  it("keeps each surface interactive through the pass-through region", () => {
    renderShell();
    act(() => toast.info("Saved."));

    // The region ignores the pointer; every card re-enables it for itself.
    expect(screen.getByRole("status").className).toContain("pointer-events-auto");
    const refresh = screen.getByRole("button", { name: "Refresh" });
    expect(refresh.closest('[class*="pointer-events-auto"]')).not.toBeNull();
  });
});
