import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/screens/ChatScreen";
import { usePrefs } from "@/stores/prefs";

// Image attachments are gated on model vision support (#633): the composer's existing
// capability check (previously local-only) now runs for any model, and a second hint
// mirrors the "can't use tools" one when an image is attached to a known non-vision model.
const mockModelDetails = vi.fn();
const mockUpload = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      models: vi.fn().mockResolvedValue([]),
      providers: vi.fn().mockResolvedValue([]),
      sessions: vi.fn().mockResolvedValue([]),
      sessionMessages: vi.fn().mockResolvedValue([]),
      suggestions: vi.fn().mockResolvedValue([]),
      modules: vi.fn().mockResolvedValue([]),
      deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
      activeRun: vi.fn().mockResolvedValue(null),
      cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
      llmPrefs: vi.fn().mockResolvedValue({
        global_default: null,
        global_embed_default: null,
        global_context_window: null,
        kv_cache_type: null,
        global_agent_max_steps: null,
        hidden: [],
      }),
      modelDetails: (model: string) => mockModelDetails(model),
      uploadAttachment: (file: File) => mockUpload(file),
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const png = () => new File(["x"], "photo.png", { type: "image/png" });

async function attachAnImage() {
  const textarea = await screen.findByLabelText("Message");
  fireEvent.paste(textarea, { clipboardData: { files: [png()] } });
  await screen.findByLabelText("Remove photo.png"); // wait for the real pill to land
}

beforeEach(() => {
  mockModelDetails.mockReset();
  mockUpload.mockReset();
  mockUpload.mockResolvedValue({ att_id: "a1", kind: "image/png", title: "photo.png" });
});

afterEach(() => {
  usePrefs.setState({ model: null });
});

describe("Chat vision-capability hint", () => {
  it("warns when an image is attached to a model that can't see it", async () => {
    usePrefs.setState({ model: "llama3.2" });
    mockModelDetails.mockResolvedValue({ capabilities: ["completion", "tools"] });
    render(<ChatScreen />, { wrapper });
    await attachAnImage();

    await waitFor(() => expect(screen.getByText(/can't see images/i)).toBeInTheDocument());
  });

  it("shows no warning when the model supports vision", async () => {
    usePrefs.setState({ model: "llama3.2" });
    mockModelDetails.mockResolvedValue({ capabilities: ["completion", "vision"] });
    render(<ChatScreen />, { wrapper });
    await attachAnImage();

    await waitFor(() => expect(mockModelDetails).toHaveBeenCalled());
    expect(screen.queryByText(/can't see images/i)).toBeNull();
  });

  it("shows no warning when capabilities are unknown (empty list)", async () => {
    usePrefs.setState({ model: "llama3.2" });
    mockModelDetails.mockResolvedValue({ capabilities: [] });
    render(<ChatScreen />, { wrapper });
    await attachAnImage();

    await waitFor(() => expect(mockModelDetails).toHaveBeenCalled());
    expect(screen.queryByText(/can't see images/i)).toBeNull();
  });

  it("shows no warning when no image is attached, even for a non-vision model", async () => {
    usePrefs.setState({ model: "llama3.2" });
    mockModelDetails.mockResolvedValue({ capabilities: ["completion", "tools"] });
    render(<ChatScreen />, { wrapper });

    await waitFor(() => expect(mockModelDetails).toHaveBeenCalled());
    expect(screen.queryByText(/can't see images/i)).toBeNull();
  });

  it("fetches capabilities for a hosted model too (previously local-only)", async () => {
    usePrefs.setState({ model: "claude/claude-3-7-sonnet-20250219" });
    mockModelDetails.mockResolvedValue({ capabilities: ["tools"] });
    render(<ChatScreen />, { wrapper });
    await attachAnImage();

    await waitFor(() =>
      expect(mockModelDetails).toHaveBeenCalledWith("claude/claude-3-7-sonnet-20250219"),
    );
    expect(await screen.findByText(/can't see images/i)).toBeInTheDocument();
  });
});
