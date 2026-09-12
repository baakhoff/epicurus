import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SavedHostedModels } from "@/screens/ModelsScreen";

const mockSavedModels = vi.fn();
const mockLlmPrefs = vi.fn();
const mockSetGlobalDefault = vi.fn();
const mockRemoveSavedModel = vi.fn();
const mockProviders = vi.fn();
const mockAddSavedModel = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    savedModels: () => mockSavedModels(),
    llmPrefs: () => mockLlmPrefs(),
    setGlobalDefault: (m: string | null) => mockSetGlobalDefault(m),
    removeSavedModel: (m: string) => mockRemoveSavedModel(m),
    providers: () => mockProviders(),
    addSavedModel: (m: string) => mockAddSavedModel(m),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
  // Shaped like the real response: capabilities/override are always present (the contract
  // defaults them), and `capabilities` is already override-resolved by the core (#711).
  mockSavedModels.mockResolvedValue([
    {
      model: "claude/claude-3-5-sonnet-latest",
      provider: "claude",
      capabilities: ["tools"],
      override: { vision: "auto", context_length: null },
    },
    {
      model: "gpt/gpt-4o",
      provider: "gpt",
      capabilities: ["tools"],
      override: { vision: "auto", context_length: null },
    },
  ]);
  mockLlmPrefs.mockResolvedValue({ global_default: "gpt/gpt-4o", hidden: [] });
  mockSetGlobalDefault.mockResolvedValue({ status: "ok" });
  mockRemoveSavedModel.mockResolvedValue({ status: "ok" });
  // The provider registry the core reports — includes the local runtime (never offered as a
  // hosted choice) plus two hosted providers, one with a key and one without.
  mockProviders.mockResolvedValue([
    { alias: "local", local: true, configured: true, needs_base_url: false, key_state: "not_required" },
    { alias: "claude", local: false, configured: true, needs_base_url: false, key_state: "present" },
    { alias: "gpt", local: false, configured: false, needs_base_url: false, key_state: "missing" },
  ]);
  mockAddSavedModel.mockResolvedValue({ status: "ok" });
});

describe("SavedHostedModels", () => {
  it("lists saved models grouped under their provider label", async () => {
    render(<SavedHostedModels />, { wrapper });
    expect(await screen.findByText("claude/claude-3-5-sonnet-latest")).toBeInTheDocument();
    expect(screen.getByText("gpt/gpt-4o")).toBeInTheDocument();
    // Grouped under readable provider names, not the raw alias. Both labels also appear as
    // options in the add-a-model select below, so match the group heading specifically.
    expect(screen.getByText("Anthropic Claude", { selector: "p" })).toBeInTheDocument();
    expect(screen.getByText("OpenAI", { selector: "p" })).toBeInTheDocument();
  });

  it("shows a compact context-window chip when the model reports one (#618)", async () => {
    mockSavedModels.mockResolvedValue([
      {
        model: "claude/claude-3-7-sonnet-20250219",
        provider: "claude",
        context_length: 200000,
        capabilities: [],
        override: { vision: "auto", context_length: null },
      },
      {
        model: "gpt/some-unlisted-model",
        provider: "gpt",
        context_length: null,
        capabilities: [],
        override: { vision: "auto", context_length: null },
      },
    ]);
    render(<SavedHostedModels />, { wrapper });
    expect(await screen.findByText("200k")).toBeInTheDocument();
    await screen.findByText("gpt/some-unlisted-model"); // the row rendered
    expect(screen.queryByText(/^\d.*[kM]$/)).toHaveTextContent("200k"); // the only chip present
  });

  it("marks the current global default and lets another be starred", async () => {
    render(<SavedHostedModels />, { wrapper });
    // gpt/gpt-4o is the stored default → its row carries the badge.
    expect(await screen.findByText("default")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /set claude\/claude-3-5-sonnet-latest as default/i }),
    );
    await waitFor(() =>
      expect(mockSetGlobalDefault).toHaveBeenCalledWith("claude/claude-3-5-sonnet-latest"),
    );
  });

  it("removes a saved model", async () => {
    render(<SavedHostedModels />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Remove gpt/gpt-4o" }));
    await waitFor(() => expect(mockRemoveSavedModel).toHaveBeenCalledWith("gpt/gpt-4o"));
  });

  it("shows an empty hint when nothing is saved", async () => {
    mockSavedModels.mockResolvedValue([]);
    render(<SavedHostedModels />, { wrapper });
    expect(await screen.findByText(/none yet — add one below/i)).toBeInTheDocument();
  });
});

describe("SavedHostedModels capability badges (#711)", () => {
  it("badges vision when the resolved capabilities include it", async () => {
    mockSavedModels.mockResolvedValue([
      {
        model: "grok/grok-latest",
        provider: "grok",
        // Resolved server-side: the operator's vision=on override already applied, so the row
        // renders the badge without knowing whether it came from the override or the catalogue.
        capabilities: ["tools", "vision"],
        override: { vision: "on", context_length: null },
      },
    ]);

    render(<SavedHostedModels />, { wrapper });

    expect(await screen.findByLabelText("Vision")).toBeInTheDocument();
    expect(screen.getByLabelText("Tools")).toBeInTheDocument();
  });

  it("shows no vision badge when the model can't see", async () => {
    mockSavedModels.mockResolvedValue([
      {
        model: "grok/grok-latest",
        provider: "grok",
        capabilities: ["tools"],
        override: { vision: "off", context_length: null },
      },
    ]);

    render(<SavedHostedModels />, { wrapper });

    expect(await screen.findByLabelText("Tools")).toBeInTheDocument();
    expect(screen.queryByLabelText("Vision")).not.toBeInTheDocument();
  });
});

describe("SavedHostedModels — add a hosted model (#922)", () => {
  it("offers only the hosted providers the core reports, never local", async () => {
    render(<SavedHostedModels />, { wrapper });
    const select = await screen.findByRole("combobox", { name: "Hosted provider" });
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toEqual(["Anthropic Claude", "OpenAI"]);
  });

  it("disables Add until a model id is entered", async () => {
    render(<SavedHostedModels />, { wrapper });
    const addButton = await screen.findByRole("button", { name: "Add" });
    expect(addButton).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Model id" }), {
      target: { value: "  " },
    });
    expect(addButton).toBeDisabled();
    fireEvent.click(addButton);
    expect(mockAddSavedModel).not.toHaveBeenCalled();
  });

  it("prepends the selected alias, keeping a two-slash OpenRouter id intact", async () => {
    mockProviders.mockResolvedValue([
      { alias: "claude", local: false, configured: true, needs_base_url: false, key_state: "present" },
      {
        alias: "openrouter",
        local: false,
        configured: true,
        needs_base_url: false,
        key_state: "present",
      },
    ]);
    render(<SavedHostedModels />, { wrapper });
    fireEvent.change(await screen.findByRole("combobox", { name: "Hosted provider" }), {
      target: { value: "openrouter" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Model id" }), {
      target: { value: "anthropic/claude-sonnet-4.6" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(mockAddSavedModel).toHaveBeenCalledWith("openrouter/anthropic/claude-sonnet-4.6"),
    );
  });

  it("surfaces the server's 400 verbatim instead of swallowing it", async () => {
    // The real `ApiError` sets `message` to the core's `detail` (see lib/api.ts); a plain Error
    // with the same message exercises the same render path without reaching into the mocked
    // module for a class it doesn't export.
    mockAddSavedModel.mockRejectedValue(new Error("not a hosted model id"));
    render(<SavedHostedModels />, { wrapper });
    fireEvent.change(await screen.findByRole("textbox", { name: "Model id" }), {
      target: { value: "bad-id" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("not a hosted model id")).toBeInTheDocument();
  });

  it("refreshes the saved list on success", async () => {
    render(<SavedHostedModels />, { wrapper });
    fireEvent.change(await screen.findByRole("textbox", { name: "Model id" }), {
      target: { value: "claude-sonnet-4-6" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mockAddSavedModel).toHaveBeenCalledWith("claude/claude-sonnet-4-6"));
    // savedModels is fetched once on mount, then re-fetched once the mutation invalidates it.
    await waitFor(() => expect(mockSavedModels.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("hints that a missing key will fail chats with this model", async () => {
    render(<SavedHostedModels />, { wrapper });
    fireEvent.change(await screen.findByRole("combobox", { name: "Hosted provider" }), {
      target: { value: "gpt" },
    });
    expect(
      await screen.findByText(/no key stored for openai.*will fail until one is/i),
    ).toBeInTheDocument();
  });

  it("hints that an unavailable key check may fail chats with this model", async () => {
    mockProviders.mockResolvedValue([
      { alias: "claude", local: false, configured: true, needs_base_url: false, key_state: "present" },
      {
        alias: "gpt",
        local: false,
        configured: true,
        needs_base_url: false,
        key_state: "unavailable",
        key_error: "OpenBao unreachable",
      },
    ]);
    render(<SavedHostedModels />, { wrapper });
    fireEvent.change(await screen.findByRole("combobox", { name: "Hosted provider" }), {
      target: { value: "gpt" },
    });
    expect(
      await screen.findByText(/key status unknown for openai \(openbao unreachable\)/i),
    ).toBeInTheDocument();
  });
});
