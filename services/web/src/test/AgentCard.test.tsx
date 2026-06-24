import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AgentCard } from "@/screens/SettingsScreen";

const mockLlmPrefs = vi.fn();
const mockSetAgentMaxSteps = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    llmPrefs: () => mockLlmPrefs(),
    setAgentMaxSteps: (value: number | null) => mockSetAgentMaxSteps(value),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const PREFS = (steps: number | null) => ({
  global_default: null,
  global_embed_default: null,
  global_context_window: null,
  global_agent_max_steps: steps,
  hidden: [],
});

beforeEach(() => {
  mockLlmPrefs.mockReset();
  mockSetAgentMaxSteps.mockReset();
  mockSetAgentMaxSteps.mockResolvedValue({ status: "ok", value: null });
});

describe("AgentCard", () => {
  it("seeds the input from the stored pref", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS(6));
    render(<AgentCard />, { wrapper });
    const input = (await screen.findByLabelText("Agent cycles")) as HTMLInputElement;
    expect(input.value).toBe("6");
  });

  it("saves an edited value on blur", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS(null));
    render(<AgentCard />, { wrapper });
    const input = await screen.findByLabelText("Agent cycles");
    fireEvent.change(input, { target: { value: "8" } });
    fireEvent.blur(input);
    await waitFor(() => expect(mockSetAgentMaxSteps).toHaveBeenCalledWith(8));
  });

  it("clears the override with Reset to default", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS(6));
    render(<AgentCard />, { wrapper });
    const reset = await screen.findByRole("button", { name: /reset to default/i });
    fireEvent.click(reset);
    await waitFor(() => expect(mockSetAgentMaxSteps).toHaveBeenCalledWith(null));
  });

  it("treats a blank field as the default (null)", async () => {
    mockLlmPrefs.mockResolvedValue(PREFS(6));
    render(<AgentCard />, { wrapper });
    const input = await screen.findByLabelText("Agent cycles");
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.blur(input);
    await waitFor(() => expect(mockSetAgentMaxSteps).toHaveBeenCalledWith(null));
  });
});
