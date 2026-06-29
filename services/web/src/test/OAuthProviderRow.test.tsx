import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { OAuthProviderRow } from "@/screens/SettingsScreen";

const mockClientStatus = vi.fn();
const mockStatus = vi.fn();
const mockModules = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    oauthClientStatus: (...a: unknown[]) => mockClientStatus(...a),
    oauthStatus: (...a: unknown[]) => mockStatus(...a),
    modules: (...a: unknown[]) => mockModules(...a),
    oauthConnect: vi.fn(),
    oauthDisconnect: vi.fn(),
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
