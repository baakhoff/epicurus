import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserView, type BrowserSource } from "@/components/archetypes/BrowserView";
import { ApiError } from "@/lib/api";
import { usePanel } from "@/stores/panel";
import { useToasts } from "@/stores/toasts";

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

/* ── Uploading into the surface (#479) ──────────────────────────────────────── */

describe("BrowserView upload (#479)", () => {
  const txt = (name: string) => new File(["body"], name, { type: "text/plain" });

  function uploadSource(send = vi.fn().mockResolvedValue({ path: "x", name: "x" })) {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        search_enabled: true,
        items: [{ id: "docs", title: "docs", nav_path: "docs" }],
      }),
      upload: { send },
    });
    return { source, send };
  }

  beforeEach(() => {
    useToasts.setState({ toasts: [] });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("offers Upload only when the source can", async () => {
    const { source } = uploadSource();
    const { unmount } = render(<BrowserView source={source} />, { wrapper });
    expect(await screen.findByRole("button", { name: "Upload" })).toBeInTheDocument();
    unmount();

    render(<BrowserView source={fakeSource()} />, { wrapper });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Upload" })).toBeNull());
  });

  it("carries the issue's picker contract on the hidden inputs", async () => {
    const { source } = uploadSource();
    render(<BrowserView source={source} />, { wrapper });
    await screen.findByRole("button", { name: "Upload" });

    const gallery = screen.getByLabelText("Photo or video files") as HTMLInputElement;
    expect(gallery.accept).toBe("image/*,video/*");
    expect(gallery.multiple).toBe(true);

    const camera = screen.getByLabelText("Camera capture") as HTMLInputElement;
    expect(camera.accept).toBe("image/*");
    expect(camera.getAttribute("capture")).toBe("environment");

    const doc = screen.getByLabelText("Document files") as HTMLInputElement;
    expect(doc.accept).toBe("");
    expect(doc.multiple).toBe(true);
  });

  it("opens the plain file dialog directly on wide screens", async () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    const clicks = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
    const { source } = uploadSource();
    render(<BrowserView source={source} />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Upload" }));
    expect(clicks).toHaveBeenCalledTimes(1);
    const picked = clicks.mock.instances[0] as HTMLInputElement;
    expect(picked.getAttribute("aria-label")).toBe("Document files");
    expect(screen.queryByRole("dialog")).toBeNull(); // no source menu on desktop
  });

  it("opens the source menu on phones; each option fires its native picker", async () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: false }));
    const clicks = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
    const { source } = uploadSource();
    render(<BrowserView source={source} />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Upload" }));
    const sheet = await screen.findByRole("dialog", { name: "Upload" });
    expect(sheet).toHaveTextContent("Photo or video");
    expect(sheet).toHaveTextContent("Camera");
    expect(sheet).toHaveTextContent("Document");

    fireEvent.click(screen.getByRole("button", { name: /Camera/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); // menu closed
    expect(clicks).toHaveBeenCalledTimes(1);
    const picked = clicks.mock.instances[0] as HTMLInputElement;
    expect(picked.getAttribute("capture")).toBe("environment");
  });

  it("uploads picked files sequentially into the current directory and refreshes", async () => {
    const order: string[] = [];
    const send = vi.fn(async (file: File, dir: string) => {
      order.push(`${dir}/${file.name}`);
      return { path: `${dir}/${file.name}`, name: file.name };
    });
    const { source } = uploadSource(send);
    render(<BrowserView source={source} />, { wrapper });

    // Drill into docs first — uploads land where the user is looking. The view shows a
    // spinner mid-navigation; wait for the toolbar to come back before picking files.
    fireEvent.click(await screen.findByText("docs"));
    await waitFor(() => expect(source.fetchPage).toHaveBeenLastCalledWith("docs", ""));
    await screen.findByRole("button", { name: "Upload" });
    const fetches = (source.fetchPage as ReturnType<typeof vi.fn>).mock.calls.length;

    fireEvent.change(screen.getByLabelText("Document files"), {
      target: { files: [txt("a.txt"), txt("b.txt")] },
    });
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(order).toEqual(["docs/a.txt", "docs/b.txt"]);
    // Each success invalidates the listing so the new entries appear with no reload.
    await waitFor(() =>
      expect(
        (source.fetchPage as ReturnType<typeof vi.fn>).mock.calls.length,
      ).toBeGreaterThan(fetches),
    );
  });

  it("renders a failed file's server detail and keeps going", async () => {
    const send = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(413, "file exceeds the 8-byte limit"))
      .mockResolvedValueOnce({ path: "big.bin", name: "big.bin" });
    const { source } = uploadSource(send);
    render(<BrowserView source={source} />, { wrapper });
    await screen.findByRole("button", { name: "Upload" });

    fireEvent.change(screen.getByLabelText("Document files"), {
      target: { files: [txt("big.bin"), txt("ok.txt")] },
    });

    await waitFor(() => expect(send).toHaveBeenCalledTimes(2)); // the failure didn't stop the batch
    expect(await screen.findByText(/file exceeds the 8-byte limit/)).toBeInTheDocument();
    expect(
      useToasts.getState().toasts.some((t) => t.message.includes("big.bin")),
    ).toBe(true);
  });

  it("uploads external file drops into the current directory", async () => {
    const { source, send } = uploadSource();
    render(<BrowserView source={source} />, { wrapper });
    const pane = await screen.findByTestId("browser-list-pane");

    const dropped = txt("dropped.txt");
    fireEvent.drop(pane, {
      dataTransfer: { types: ["Files"], files: [dropped], dropEffect: "" },
    });
    await waitFor(() => expect(send).toHaveBeenCalledWith(dropped, ""));
  });
});

/* ── Deleting from the surface (#564) ───────────────────────────────────────── */

describe("BrowserView delete (#564)", () => {
  beforeEach(() => {
    useToasts.setState({ toasts: [] });
  });

  function deleteSource(items: unknown[], remove = vi.fn().mockResolvedValue({ deleted: true })) {
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({ title: "Files", items }),
      remove,
    });
    return { source, remove };
  }

  it("deletes a file only after the Confirm is accepted", async () => {
    const { source, remove } = deleteSource([
      { id: "notes/draft.md", title: "draft.md", href: "/dl?path=notes/draft.md", deletable: true },
    ]);
    render(<BrowserView source={source} />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Delete draft.md" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/Delete "draft\.md"\?/);
    // Nothing happens until the destructive action is confirmed.
    expect(remove).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("notes/draft.md"));
  });

  it("warns that a folder's contents go too (recursion)", async () => {
    const { source, remove } = deleteSource([
      { id: "proj", title: "proj", nav_path: "proj", deletable: true },
    ]);
    render(<BrowserView source={source} />, { wrapper });

    // The folder's Delete button lives in its row — a folder never opens a detail pane.
    fireEvent.click(await screen.findByRole("button", { name: "Delete proj" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/everything inside it/i);

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("proj"));
  });

  it("does not delete when the Confirm is cancelled", async () => {
    const { source, remove } = deleteSource([
      { id: "a.txt", title: "a.txt", href: "/dl?path=a.txt", deletable: true },
    ]);
    render(<BrowserView source={source} />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Delete a.txt" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(remove).not.toHaveBeenCalled();
  });

  it("hides the delete affordance where the ownership rule forbids it", async () => {
    // A remover is wired, but a module-owned entry is not `deletable` → no button.
    const { source } = deleteSource([
      { id: "knowledge/x.md", title: "x.md", href: "/dl?path=knowledge/x.md", deletable: false },
    ]);
    render(<BrowserView source={source} />, { wrapper });
    await screen.findByText("x.md");
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
  });

  it("offers no delete when the source cannot remove (e.g. a module page)", async () => {
    // `deletable` is set but no `remove` is wired — module browser pages never get delete.
    const source = fakeSource({
      fetchPage: vi.fn().mockResolvedValue({
        title: "Files",
        items: [{ id: "a.txt", title: "a.txt", deletable: true }],
      }),
    });
    render(<BrowserView source={source} />, { wrapper });
    await screen.findByText("a.txt");
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
  });

  it("surfaces a server refusal as a toast and closes the dialog", async () => {
    const remove = vi
      .fn()
      .mockRejectedValue(new ApiError(400, "'knowledge' belongs to the knowledge module"));
    const { source } = deleteSource(
      [{ id: "a.txt", title: "a.txt", href: "/dl?path=a.txt", deletable: true }],
      remove,
    );
    render(<BrowserView source={source} />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Delete a.txt" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(
      useToasts.getState().toasts.some((t) => t.message.includes("Could not delete")),
    ).toBe(true);
  });
});
