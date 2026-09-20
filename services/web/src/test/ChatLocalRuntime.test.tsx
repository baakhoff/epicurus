/**
 * Chat against a deployment that runs no local runtime (#962).
 *
 * The model picker used to open on a "Local" heading whatever the deployment was, with
 * "local runtime unreachable" under it when the model list failed — a sentence that describes
 * a fault on a deployment where there is deliberately nothing to reach. The heading goes; the
 * core-default row stays, because the core's default may well be a hosted model and this is the
 * only way back to it.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatScreen } from "@/screens/ChatScreen";
import { usePrefs } from "@/stores/prefs";

const mockModels = vi.fn();
const mockLocalRuntime = vi.fn();
const mockProviders = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    models: () => mockModels(),
    localRuntime: () => mockLocalRuntime(),
    providers: () => mockProviders(),
    savedModels: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue([]),
    sessionMessages: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
    activeRun: vi.fn().mockResolvedValue(null),
    cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
    modules: vi.fn().mockResolvedValue([]),
    modelDetails: vi.fn().mockResolvedValue({ capabilities: [] }),
    setSessionModel: vi.fn().mockResolvedValue({ status: "ok" }),
    addSavedModel: vi.fn().mockResolvedValue({ status: "ok" }),
    llmPrefs: vi.fn().mockResolvedValue({
      global_default: null,
      global_embed_default: null,
      global_context_window: null,
      kv_cache_type: null,
      global_agent_max_steps: null,
      hidden: [],
    }),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

/** Open the model picker sheet from the composer header. */
async function openPicker() {
  fireEvent.click(await screen.findByRole("button", { name: /default model/i }));
  await screen.findByText("Model for this chat");
}

beforeEach(() => {
  vi.clearAllMocks();
  mockModels.mockResolvedValue([]);
  mockProviders.mockResolvedValue([]);
  mockLocalRuntime.mockResolvedValue({ state: "ok", url_configured: true });
  usePrefs.setState({ model: null });
});

afterEach(() => {
  usePrefs.setState({ model: null });
});

describe("Chat model picker — no local runtime", () => {
  beforeEach(() => {
    mockLocalRuntime.mockResolvedValue({ state: "absent", url_configured: false });
  });

  it("shows no Local heading and no unreachable warning", async () => {
    render(<ChatScreen />, { wrapper });
    await openPicker();

    expect(screen.queryByText("Local")).toBeNull();
    expect(screen.queryByText(/local runtime unreachable/i)).toBeNull();
    // The hosted half is the whole picker here, and it still explains itself.
    expect(screen.getByText("Hosted")).toBeInTheDocument();
  });

  it("still offers the core default — it may well be a hosted model", async () => {
    render(<ChatScreen />, { wrapper });
    await openPicker();

    expect(screen.getByRole("button", { name: /core default/i })).toBeInTheDocument();
  });
});

describe("Chat model picker — a runtime that is there", () => {
  it("keeps the Local heading when the runtime is ok", async () => {
    mockModels.mockResolvedValue([
      { name: "llama3.2:latest", size: 1, loaded: false, hidden: false, capabilities: [] },
    ]);
    render(<ChatScreen />, { wrapper });
    await openPicker();

    expect(await screen.findByText("Local")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /llama3\.2:latest/ })).toBeInTheDocument();
  });

  it("warns when the runtime is unreachable, even though the model list answered 200", async () => {
    // The pin: since #962 `/llm/models` no longer errors, so a warning driven by that query
    // failing would simply never appear again.
    mockLocalRuntime.mockResolvedValue({ state: "unreachable", url_configured: true });
    mockModels.mockResolvedValue([]);
    render(<ChatScreen />, { wrapper });
    await openPicker();

    expect(await screen.findByText(/local runtime unreachable/i)).toBeInTheDocument();
    expect(screen.getByText("Local")).toBeInTheDocument();
  });
});

describe("Chat first run", () => {
  it("does not tell a hosted-only deployment to pull a local model", async () => {
    mockLocalRuntime.mockResolvedValue({ state: "absent", url_configured: false });
    render(<ChatScreen />, { wrapper });

    await waitFor(() =>
      expect(screen.getByText(/This deployment runs no local AI/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /pull llama3\.2/i })).toBeNull();
  });

  it("still offers a pull on a deployment that has a runtime", async () => {
    render(<ChatScreen />, { wrapper });

    expect(await screen.findByRole("button", { name: /pull llama3\.2/i })).toBeInTheDocument();
  });
});
