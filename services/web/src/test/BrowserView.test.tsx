import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserView, type BrowserSource } from "@/components/archetypes/BrowserView";
import { usePanel } from "@/stores/panel";

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

/** A fake adapter whose functions are spies the tests assert against. */
function fakeSource(overrides: Partial<BrowserSource> = {}): BrowserSource {
  return {
    queryKey: ["test-browser"],
    fetchPage: vi.fn().mockResolvedValue({ items: [] }),
    readText: vi.fn().mockResolvedValue({ path: "", name: "", content: "" }),
    move: vi.fn().mockResolvedValue({ path: "" }),
    ...overrides,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  usePanel.getState().close();
});

describe("BrowserView", () => {
  it("renders the source's items, then reveals detail on select", async () => {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Echoes",
        items: [
          { id: "hello", title: "hello", subtitle: "a friendly echo", body: "hello — echoed back." },
          { id: "quote", title: "abundance", body: "the quote" },
        ],
      }),
    });
    render(<BrowserView source={source} />, { wrapper });

    expect(await screen.findByText("hello")).toBeInTheDocument();
    expect(screen.getByText("abundance")).toBeInTheDocument();

    fireEvent.click(screen.getByText("hello"));
    expect(await screen.findByText("hello — echoed back.")).toBeInTheDocument();
  });

  it("navigates up one level from a subdirectory (#338)", async () => {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        items: [{ id: "docs", title: "docs", nav_path: "docs/sub" }],
      }),
    });
    render(<BrowserView source={source} />, { wrapper });

    // Drill into docs/sub, which surfaces the breadcrumb toolbar (and the up control).
    fireEvent.click(await screen.findByText("docs"));
    await waitFor(() => expect(source.fetchPage).toHaveBeenLastCalledWith("docs/sub", ""));

    // Up one level → the parent directory ("docs"). The control appears once the
    // sub-directory finishes loading (the browser shows a spinner mid-navigation).
    fireEvent.click(await screen.findByRole("button", { name: /up one level/i }));
    await waitFor(() => expect(source.fetchPage).toHaveBeenLastCalledWith("docs", ""));
  });

  it("fetches the initial listing at the root with no query", async () => {
    const source = fakeSource();
    render(<BrowserView source={source} />, { wrapper });
    await screen.findByText(/nothing here yet/i);
    expect(source.fetchPage).toHaveBeenCalledWith("", "");
  });

  it("opens a text file in the split-screen reader (#KB-refactor)", async () => {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        items: [{ id: "kb/a.md", title: "a.md", subtitle: "1 KB", href: "/dl?path=kb/a.md" }],
      }),
      readText: vi.fn().mockResolvedValue({ path: "kb/a.md", name: "a.md", content: "# Hi" }),
    });
    render(<BrowserView source={source} />, { wrapper });

    fireEvent.click(await screen.findByText("a.md"));
    fireEvent.click(await screen.findByRole("button", { name: /open/i }));

    await waitFor(() => expect(source.readText).toHaveBeenCalledWith("kb/a.md"));
    await waitFor(() => expect(usePanel.getState().stack.at(-1)?.view).toBe("doc-reader"));
  });

  it("offers Download but not Open for a non-text file", async () => {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        items: [{ id: "img.png", title: "img.png", href: "/dl?path=img.png" }],
      }),
    });
    render(<BrowserView source={source} />, { wrapper });
    fireEvent.click(await screen.findByText("img.png"));
    expect(await screen.findByText("Download")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open/i })).toBeNull();
  });

  it("renames a movable file in place (#381)", async () => {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        items: [
          { id: "notes/draft.md", title: "draft.md", href: "/dl?path=notes/draft.md", movable: true },
        ],
      }),
      move: vi.fn().mockResolvedValue({ path: "notes/final.md" }),
    });
    render(<BrowserView source={source} />, { wrapper });

    fireEvent.click(await screen.findByText("draft.md"));
    fireEvent.click(await screen.findByRole("button", { name: /rename/i }));
    fireEvent.change(screen.getByLabelText("New name"), { target: { value: "final.md" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(source.move).toHaveBeenCalledWith("notes/draft.md", "notes/final.md"),
    );
  });

  it("offers no rename on a read-only (non-movable) entry", async () => {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        items: [{ id: "docs/readme.txt", title: "readme.txt", href: "/dl?path=docs/readme.txt" }],
      }),
    });
    render(<BrowserView source={source} />, { wrapper });
    fireEvent.click(await screen.findByText("readme.txt"));
    expect(await screen.findByText("Download")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /rename/i })).toBeNull();
  });

  it("guards a folder tap against an immediate double-fire (Android PWA, #428)", async () => {
    const fetchPage = vi.fn().mockResolvedValue({
      title: "Files",
      items: [{ id: "docs", title: "docs", nav_path: "docs" }],
    });
    const source = fakeSource({ fetchPage });
    render(<BrowserView source={source} />, { wrapper });

    const folder = await screen.findByText("docs");
    // Both dispatches inside one act() so the second fires before the first's
    // navigation commits and unmounts the row — reproducing a touchend+click
    // double-fire on the same element, not two clicks on two different renders.
    act(() => {
      fireEvent.click(folder);
      fireEvent.click(folder);
    });

    await waitFor(() => expect(fetchPage).toHaveBeenCalledWith("docs", ""));
    expect(fetchPage.mock.calls.filter((c) => c[0] === "docs")).toHaveLength(1);
  });

  it("moves a file by dragging it onto a folder (#391)", async () => {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        items: [
          { id: "a.md", title: "a.md", href: "/dl?path=a.md", movable: true },
          { id: "docs", title: "docs", nav_path: "docs" },
        ],
      }),
      move: vi.fn().mockResolvedValue({ path: "docs/a.md" }),
    });
    render(<BrowserView source={source} />, { wrapper });

    const file = (await screen.findByText("a.md")).closest("button")!;
    const folder = screen.getByText("docs").closest("button")!;
    const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(file, { dataTransfer });
    fireEvent.dragOver(folder, { dataTransfer });
    fireEvent.drop(folder, { dataTransfer });

    await waitFor(() => expect(source.move).toHaveBeenCalledWith("a.md", "docs/a.md"));
  });
});
