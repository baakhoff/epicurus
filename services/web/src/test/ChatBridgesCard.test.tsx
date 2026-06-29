import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatBridgesCard } from "@/components/ChatBridgesCard";

const mockBridges = vi.fn();
const mockConnect = vi.fn();
const mockSetEnabled = vi.fn();
const mockDisconnect = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    messagingBridges: (...a: unknown[]) => mockBridges(...a),
    connectBridge: (...a: unknown[]) => mockConnect(...a),
    setBridgeEnabled: (...a: unknown[]) => mockSetEnabled(...a),
    disconnectBridge: (...a: unknown[]) => mockDisconnect(...a),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const DISCORD_OFF = {
  bridge: "discord",
  label: "Discord",
  manageable: true,
  configured: false,
  enabled: true,
  connected: false,
  detail: "no bot token set",
};
const DISCORD_ON = {
  bridge: "discord",
  label: "Discord",
  manageable: true,
  configured: true,
  enabled: true,
  connected: true,
  detail: "1 server · 3 channels",
};
const LOOPBACK = {
  bridge: "loopback",
  label: "Loopback (dev echo)",
  manageable: false,
  configured: true,
  enabled: true,
  connected: true,
  detail: "in-process echo · 0 delivered",
};

beforeEach(() => {
  mockBridges.mockReset();
  mockConnect.mockReset().mockResolvedValue({});
  mockSetEnabled.mockReset().mockResolvedValue({});
  mockDisconnect.mockReset().mockResolvedValue({});
});

describe("ChatBridgesCard (#369)", () => {
  it("lists manageable bridges and hides the in-process loopback", async () => {
    mockBridges.mockResolvedValue([DISCORD_OFF, LOOPBACK]);
    render(<ChatBridgesCard />, { wrapper });

    expect(await screen.findByText("Discord")).toBeInTheDocument();
    expect(screen.queryByText(/loopback/i)).not.toBeInTheDocument();
  });

  it("connects a bridge: revealing the form and saving a token calls connectBridge", async () => {
    mockBridges.mockResolvedValue([DISCORD_OFF]);
    render(<ChatBridgesCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /connect/i }));
    fireEvent.change(screen.getByPlaceholderText(/bot token/i), {
      target: { value: "tok-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save & connect/i }));

    await waitFor(() => expect(mockConnect).toHaveBeenCalledWith("discord", "tok-123"));
  });

  it("a connected bridge shows on/off + disconnect, wired to the API", async () => {
    mockBridges.mockResolvedValue([DISCORD_ON]);
    render(<ChatBridgesCard />, { wrapper });

    // The status detail surfaces the live reach.
    expect(await screen.findByText("1 server · 3 channels")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: /discord/i }));
    await waitFor(() => expect(mockSetEnabled).toHaveBeenCalledWith("discord", false));

    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(mockDisconnect).toHaveBeenCalledWith("discord"));
  });

  it("hides the whole card when the messaging module is unavailable", async () => {
    mockBridges.mockRejectedValue(new Error("no reachable module named 'messaging'"));
    render(<ChatBridgesCard />, { wrapper });

    await waitFor(() => expect(mockBridges).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByText(/chat bridges/i)).not.toBeInTheDocument(),
    );
  });
});
