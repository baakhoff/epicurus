import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantInstructionsCard } from "@/screens/SettingsScreen";

const mockAgentInstructions = vi.fn();
const mockSetAgentInstructions = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    agentInstructions: () => mockAgentInstructions(),
    setAgentInstructions: (v: string | null) => mockSetAgentInstructions(v),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAgentInstructions.mockResolvedValue({ instructions: "You are epsilon.", is_default: true });
  mockSetAgentInstructions.mockResolvedValue({ status: "ok", is_default: false });
});

describe("AssistantInstructionsCard", () => {
  it("prefills the textarea with the effective prompt and notes the default", async () => {
    render(<AssistantInstructionsCard />, { wrapper });
    const box = (await screen.findByLabelText("Assistant instructions")) as HTMLTextAreaElement;
    expect(box.value).toBe("You are epsilon.");
    expect(screen.getByText(/using the shipped default/i)).toBeInTheDocument();
    // Save is disabled until the operator actually edits the prompt.
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves an edited prompt", async () => {
    render(<AssistantInstructionsCard />, { wrapper });
    const box = await screen.findByLabelText("Assistant instructions");
    fireEvent.change(box, { target: { value: "Be extremely terse." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockSetAgentInstructions).toHaveBeenCalledWith("Be extremely terse."));
  });

  it("resets to the shipped default (saves null)", async () => {
    // A custom prompt is stored → the Reset control is offered.
    mockAgentInstructions.mockResolvedValue({ instructions: "Custom.", is_default: false });
    render(<AssistantInstructionsCard />, { wrapper });
    await screen.findByDisplayValue("Custom.");
    fireEvent.click(screen.getByRole("button", { name: /reset to default/i }));
    await waitFor(() => expect(mockSetAgentInstructions).toHaveBeenCalledWith(null));
  });

  it("warns when the prompt grows past the soft size limit", async () => {
    render(<AssistantInstructionsCard />, { wrapper });
    const box = await screen.findByLabelText("Assistant instructions");
    fireEvent.change(box, { target: { value: "x".repeat(4100) } });
    expect(screen.getByText(/never trimmed/i)).toBeInTheDocument();
  });
});
