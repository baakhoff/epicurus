import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { OAuthProviderRow } from "@/screens/SettingsScreen";

const mockClientStatus = vi.fn();
const mockStatus = vi.fn();
const mockModules = vi.fn();
const mockDisconnect = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    oauthClientStatus: (...a: unknown[]) => mockClientStatus(...a),
    oauthStatus: (...a: unknown[]) => mockStatus(...a),
    modules: (...a: unknown[]) => mockModules(...a),
    oauthConnect: vi.fn(),
    oauthDisconnect: (...a: unknown[]) => mockDisconnect(...a),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

// #393: on a phone the connected-account row overflowed because of the two text action
// buttons. They are now icon-only (label moved to aria-label + the shared Tooltip) so the
// row fits; the primary "Connect" CTA keeps its label.
describe("OAuthProviderRow (#393)", () => {
  it("renders credential + disconnect actions icon-only (label via aria/tooltip) when connected", async () => {
    mockClientStatus.mockResolvedValue({ configured: true });
    mockStatus.mockResolvedValue({ connected: true, scope: "a b c" });
    mockModules.mockResolvedValue([]);

    render(<OAuthProviderRow providerId="google" />, { wrapper });

    // The accessible name comes from aria-label, not visible text…
    const update = await screen.findByRole("button", { name: "Update credentials" });
    const disconnect = await screen.findByRole("button", { name: "Disconnect" });
    // …and the buttons carry no text label — just their lucide icon (icon-only).
    expect(update.textContent).toBe("");
    expect(disconnect.textContent).toBe("");
    // The label stays discoverable via the shared Tooltip (always in the DOM, faded).
    expect(screen.getAllByRole("tooltip").map((t) => t.textContent)).toEqual(
      expect.arrayContaining(["Update credentials", "Disconnect"]),
    );
  });

  it("keeps the Connect button labeled and makes Add-credentials icon-only when not connected", async () => {
    mockClientStatus.mockResolvedValue({ configured: false });
    mockStatus.mockResolvedValue({ connected: false });
    mockModules.mockResolvedValue([]);

    render(<OAuthProviderRow providerId="google" />, { wrapper });

    expect(await screen.findByRole("button", { name: /connect/i })).toHaveTextContent("Connect");
    expect(screen.getByRole("button", { name: "Add credentials" }).textContent).toBe("");
  });
});

/* ── graceful global disconnect (#764) ──────────────────────────────────────── */
// Disconnecting reaches far past this row: the core strips the provider from every module's
// stored selection (#209). The caches describing what the modules can see must not survive it
// — several are deliberately long-lived (calendar's account view: 5 min; the mailbox list:
// 30 s), so left alone they keep painting Google chips on the next page the operator opens.

describe("OAuthProviderRow disconnect (#764)", () => {
  /** Renders the row against a client we can interrogate, and returns the spy on invalidation. */
  async function disconnectAndCaptureInvalidations() {
    mockClientStatus.mockResolvedValue({ configured: true });
    mockStatus.mockResolvedValue({ connected: true, scope: "a b" });
    mockModules.mockResolvedValue([]);
    mockDisconnect.mockResolvedValue({ status: "disconnected" });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    render(
      <QueryClientProvider client={qc}>
        <OAuthProviderRow providerId="google" />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(mockDisconnect).toHaveBeenCalledWith("google"));
    return invalidate;
  }

  it("clears every module-facing cache, not just this row's status", async () => {
    const invalidate = await disconnectAndCaptureInvalidations();
    const keys = () =>
      invalidate.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey));
    await waitFor(() => expect(keys()).toContain(JSON.stringify(["oauth-status", "google"])));
    for (const key of ["modules", "module-collections", "module-status", "module-page"]) {
      expect(keys()).toContain(JSON.stringify([key]));
    }
  });

  it("invalidates by key prefix so every module's page and account view is covered", async () => {
    // A per-module key (["module-collections", "calendar"]) would leave every other module
    // stale — react-query matches by prefix, so the bare key is the one that reaches them all.
    const invalidate = await disconnectAndCaptureInvalidations();
    await waitFor(() =>
      expect(
        invalidate.mock.calls.some(
          (c) =>
            JSON.stringify((c[0] as { queryKey: unknown }).queryKey) ===
            JSON.stringify(["module-collections"]),
        ),
      ).toBe(true),
    );
  });
});
