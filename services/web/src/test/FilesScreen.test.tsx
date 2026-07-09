import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FilesScreen } from "@/screens/FilesScreen";

// The Files surface is core-owned (ADR-0063): it must back BrowserView with the core
// file-space endpoints, not the module page proxy.
const mockFilesPage = vi.fn();
const mockFilesRead = vi.fn();
const mockFilesMove = vi.fn();
const mockFilesDelete = vi.fn();
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
    filesPage: (...args: unknown[]) => mockFilesPage(...args),
    filesRead: (...args: unknown[]) => mockFilesRead(...args),
    filesMove: (...args: unknown[]) => mockFilesMove(...args),
    filesDelete: (...args: unknown[]) => mockFilesDelete(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockFilesPage.mockReset();
  mockFilesRead.mockReset();
  mockFilesMove.mockReset();
  mockFilesDelete.mockReset();
});

describe("FilesScreen", () => {
  it("renders the Files header and lists items from the core file-space endpoint", async () => {
    mockFilesPage.mockResolvedValue({
      title: "Files",
      items: [{ id: "readme.md", title: "readme.md", href: "/platform/v1/files/download?path=readme.md" }],
    });
    render(<FilesScreen />, { wrapper });

    expect(screen.getByRole("heading", { name: "Files" })).toBeInTheDocument();
    expect(await screen.findByText("readme.md")).toBeInTheDocument();
    // It fetches through the core file-space source, not a module page proxy.
    expect(mockFilesPage).toHaveBeenCalledWith("", "");
  });

  it("wires delete to the core files delete endpoint (#564)", async () => {
    mockFilesPage.mockResolvedValue({
      title: "Files",
      items: [
        {
          id: "old.txt",
          title: "old.txt",
          href: "/platform/v1/files/download?path=old.txt",
          deletable: true,
        },
      ],
    });
    mockFilesDelete.mockResolvedValue({ deleted: true });
    render(<FilesScreen />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Delete old.txt" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockFilesDelete).toHaveBeenCalledWith("old.txt"));
  });
});
