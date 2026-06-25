import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserView } from "@/components/archetypes/BrowserView";
import { usePanel } from "@/stores/panel";

const mockModulePage = vi.fn();
const mockReadText = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    readModuleText: (...args: unknown[]) => mockReadText(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockModulePage.mockReset();
  mockReadText.mockReset();
  usePanel.getState().close();
});

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

  it("navigates up one level from a subdirectory (#338)", async () => {
    mockModulePage.mockResolvedValue({
      title: "Files",
      items: [{ id: "docs", title: "docs", nav_path: "docs/sub" }],
    });
    render(<BrowserView module="storage" pageId="files" />, { wrapper });

    // Drill into docs/sub, which surfaces the breadcrumb toolbar (and the up control).
    fireEvent.click(await screen.findByText("docs"));
    await waitFor(() =>
      expect(mockModulePage).toHaveBeenLastCalledWith("storage", "files", { path: "docs/sub" }),
    );

    // Up one level → the parent directory ("docs"). The control appears once the
    // sub-directory finishes loading (the browser shows a spinner mid-navigation).
    fireEvent.click(await screen.findByRole("button", { name: /up one level/i }));
    await waitFor(() =>
      expect(mockModulePage).toHaveBeenLastCalledWith("storage", "files", { path: "docs" }),
    );
  });

  it("fetches the page through the core proxy by module + page id", async () => {
    mockModulePage.mockResolvedValue({ items: [] });
    render(<BrowserView module="files" pageId="browse" />, { wrapper });
    await screen.findByText(/nothing here yet/i);
    expect(mockModulePage).toHaveBeenCalledWith("files", "browse", undefined);
  });

  it("opens a text file in the split-screen reader (#KB-refactor)", async () => {
    mockModulePage.mockResolvedValue({
      title: "Files",
      items: [{ id: "kb/a.md", title: "a.md", subtitle: "1 KB", href: "/dl?path=kb/a.md" }],
    });
    mockReadText.mockResolvedValue({ path: "kb/a.md", name: "a.md", content: "# Hi" });
    render(<BrowserView module="storage" pageId="files" />, { wrapper });

    fireEvent.click(await screen.findByText("a.md"));
    fireEvent.click(await screen.findByRole("button", { name: /open/i }));

    await waitFor(() => expect(mockReadText).toHaveBeenCalledWith("storage", "kb/a.md"));
    await waitFor(() => expect(usePanel.getState().stack.at(-1)?.view).toBe("doc-reader"));
  });

  it("offers Download but not Open for a non-text file", async () => {
    mockModulePage.mockResolvedValue({
      title: "Files",
      items: [{ id: "img.png", title: "img.png", href: "/dl?path=img.png" }],
    });
    render(<BrowserView module="storage" pageId="files" />, { wrapper });
    fireEvent.click(await screen.findByText("img.png"));
    expect(await screen.findByText("Download")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open/i })).toBeNull();
  });
});
