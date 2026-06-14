import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserView } from "@/components/archetypes/BrowserView";

const mockModulePage = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { modulePage: (...args: unknown[]) => mockModulePage(...args) },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => mockModulePage.mockReset());

describe("BrowserView", () => {
  it("renders the module's items, then reveals detail on select", async () => {
    mockModulePage.mockResolvedValue({
      title: "Echoes",
      items: [
        { id: "hello", title: "hello", subtitle: "a friendly echo", body: "hello — echoed back." },
        { id: "quote", title: "abundance", body: "the quote" },
      ],
    });
    render(<BrowserView module="echo" pageId="echoes" />, { wrapper });

    expect(await screen.findByText("hello")).toBeInTheDocument();
    expect(screen.getByText("abundance")).toBeInTheDocument();

    fireEvent.click(screen.getByText("hello"));
    expect(await screen.findByText("hello — echoed back.")).toBeInTheDocument();
  });

  it("fetches the page through the core proxy by module + page id", async () => {
    mockModulePage.mockResolvedValue({ items: [] });
    render(<BrowserView module="files" pageId="browse" />, { wrapper });
    await screen.findByText(/nothing here yet/i);
    expect(mockModulePage).toHaveBeenCalledWith("files", "browse");
  });
});
