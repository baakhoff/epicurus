import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, detailText } from "@/lib/api";
import { ModuleSnapshot } from "@/lib/contracts";
import { ModulesScreen } from "@/screens/ModulesScreen";

const mockModules = vi.fn();
const mockModuleConfig = vi.fn();
const mockSaveModuleConfig = vi.fn();
const mockProviders = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      modules: () => mockModules(),
      moduleConfig: (name: string) => mockModuleConfig(name),
      saveModuleConfig: (name: string, values: unknown) => mockSaveModuleConfig(name, values),
      providers: () => mockProviders(),
      dockerStatus: async () => ({ available: true, reason: null }),
      getModuleModels: async () => ({ models: {} }),
      getModuleCollections: async () => null,
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const WEBSEARCH = ModuleSnapshot.parse({
  manifest: {
    name: "websearch",
    version: "0.5.0",
    ui: {
      summary: "web search",
      config_schema: {
        type: "object",
        properties: {
          websearch_backend: {
            type: "string",
            title: "Search provider",
            enum: ["searxng", "openrouter"],
            enumLabels: ["SearXNG (self-hosted)", "OpenRouter web search"],
            enumRequiresProviderKey: [null, "openrouter"],
            default: "searxng",
          },
        },
      },
    },
  },
  status: { healthy: true, version: "0.5.0" },
  enabled: true,
  disabled_tools: [],
});

function provider(alias: string, key_state: string) {
  return { alias, local: alias === "local", configured: key_state !== "missing", needs_base_url: false, key_state };
}

async function openSettings() {
  fireEvent.click(await screen.findByRole("button", { name: /expand/i }));
  return (await screen.findByRole("combobox", { name: "Search provider" })) as HTMLSelectElement;
}

beforeEach(() => {
  mockModules.mockReset().mockResolvedValue([WEBSEARCH]);
  mockModuleConfig.mockReset().mockResolvedValue({});
  mockSaveModuleConfig.mockReset().mockResolvedValue({ status: "ok" });
  mockProviders.mockReset();
});

describe("Modules page: an option gated on a provider key (#984)", () => {
  it("locks OpenRouter until an OpenRouter key is stored", async () => {
    mockProviders.mockResolvedValue([provider("local", "not_required"), provider("openrouter", "missing")]);
    render(<ModulesScreen />, { wrapper });
    await openSettings();

    const option = screen.getByRole("option", { name: /OpenRouter web search/ }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    expect(screen.getByText(/add your OpenRouter API key on the Models page/)).toBeInTheDocument();
  });

  it("unlocks it once the providers list reports the key present, and saves the choice", async () => {
    mockProviders.mockResolvedValue([provider("openrouter", "present")]);
    render(<ModulesScreen />, { wrapper });
    const select = await openSettings();

    const option = screen.getByRole("option", { name: /OpenRouter web search/ }) as HTMLOptionElement;
    expect(option.disabled).toBe(false);
    fireEvent.change(select, { target: { value: "openrouter" } });
    fireEvent.click(screen.getByRole("button", { name: /save settings/i }));
    await waitFor(() =>
      expect(mockSaveModuleConfig).toHaveBeenCalledWith("websearch", { websearch_backend: "openrouter" }),
    );
  });

  it("an unreadable key store does not unlock the option", async () => {
    mockProviders.mockResolvedValue([provider("openrouter", "unavailable")]);
    render(<ModulesScreen />, { wrapper });
    await openSettings();
    expect((screen.getByRole("option", { name: /OpenRouter web search/ }) as HTMLOptionElement).disabled).toBe(
      true,
    );
  });

  it("shows the core's refusal sentence, not [object Object]", async () => {
    // The key was removed between the page load and the save: the core refuses server-side.
    mockProviders.mockResolvedValue([provider("openrouter", "present")]);
    mockSaveModuleConfig.mockRejectedValue(
      new ApiError(
        409,
        detailText({
          code: "provider_key_required",
          provider: "openrouter",
          message: "'Search provider' can only be set to 'openrouter' once an API key is stored.",
        })!,
      ),
    );
    render(<ModulesScreen />, { wrapper });
    const select = await openSettings();
    fireEvent.change(select, { target: { value: "openrouter" } });
    fireEvent.click(screen.getByRole("button", { name: /save settings/i }));
    expect(await screen.findByText(/can only be set to 'openrouter' once an API key is stored/)).toBeInTheDocument();
    expect(screen.queryByText(/object Object/)).toBeNull();
  });

  it("never fetches the provider list for a module whose form has no gated option", async () => {
    mockModules.mockResolvedValue([
      ModuleSnapshot.parse({
        manifest: {
          name: "echo",
          version: "0.1.0",
          ui: { summary: "e", config_schema: { type: "object", properties: { greeting: { type: "string" } } } },
        },
        status: { healthy: true, version: "0.1.0" },
        enabled: true,
        disabled_tools: [],
      }),
    ]);
    render(<ModulesScreen />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: /expand/i }));
    await screen.findByRole("button", { name: /save settings/i });
    expect(mockProviders).not.toHaveBeenCalled();
  });
});

describe("detailText", () => {
  it("reads a string, a structured message, and nothing else", () => {
    expect(detailText("plain")).toBe("plain");
    expect(detailText({ code: "x", message: "readable" })).toBe("readable");
    expect(detailText({ code: "x" })).toBeUndefined();
    expect(detailText([{ loc: ["body"], msg: "field required" }])).toBeUndefined();
    expect(detailText(null)).toBeUndefined();
  });
});
