import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SchemaForm, type ObjectSchema } from "@/components/SchemaForm";

// The websearch module's backend field (#984), as its manifest declares it.
const SCHEMA = {
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
} as ObjectSchema;

function option(name: RegExp): HTMLOptionElement {
  return screen.getByRole("option", { name }) as HTMLOptionElement;
}

describe("SchemaForm provider-key gating (#984)", () => {
  it("disables an option whose provider key is not stored, and says where to add one", () => {
    render(<SchemaForm schema={SCHEMA} onSubmit={() => {}} storedProviderKeys={new Set(["local"])} />);

    expect(option(/OpenRouter web search/).disabled).toBe(true);
    expect(option(/OpenRouter web search/).textContent).toMatch(/needs your OpenRouter key/);
    expect(option(/SearXNG/).disabled).toBe(false);
    expect(
      screen.getByText(/To use "OpenRouter web search", add your OpenRouter API key on the Models page/),
    ).toBeInTheDocument();
    // A hint, not an error: nothing is wrong with the stored value.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("enables the option once the key is stored, with no hint", () => {
    render(
      <SchemaForm schema={SCHEMA} onSubmit={() => {}} storedProviderKeys={new Set(["openrouter"])} />,
    );
    expect(option(/OpenRouter web search/).disabled).toBe(false);
    expect(option(/OpenRouter web search/).textContent).toBe("OpenRouter web search");
    expect(screen.queryByText(/Models page/)).toBeNull();

    // …and it can be chosen and submitted.
    const onSubmit = vi.fn();
    render(
      <SchemaForm schema={SCHEMA} onSubmit={onSubmit} storedProviderKeys={new Set(["openrouter"])} />,
    );
    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[selects.length - 1], { target: { value: "openrouter" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).at(-1)!);
    expect(onSubmit).toHaveBeenCalledWith({ websearch_backend: "openrouter" });
  });

  it("flags a stored choice whose key has since been removed, without hiding it", () => {
    render(
      <SchemaForm
        schema={SCHEMA}
        initial={{ websearch_backend: "openrouter" }}
        onSubmit={() => {}}
        storedProviderKeys={new Set()}
      />,
    );
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("openrouter");
    // Still selectable so the form shows the truth, but called out as unable to run.
    expect(option(/OpenRouter web search/).disabled).toBe(false);
    expect(screen.getByRole("alert").textContent).toMatch(
      /No OpenRouter API key is stored, so "OpenRouter web search" cannot run/,
    );
  });

  it("does no gating at all when the caller passes no stored keys (action forms)", () => {
    render(<SchemaForm schema={SCHEMA} onSubmit={() => {}} />);
    expect(option(/OpenRouter web search/).disabled).toBe(false);
    expect(screen.queryByText(/Models page/)).toBeNull();
  });
});
