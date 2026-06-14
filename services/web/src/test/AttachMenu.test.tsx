import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AttachButton, AttachmentPill } from "@/components/AttachMenu";

const mockUpload = vi.fn();
const mockSessions = vi.fn();
const mockModules = vi.fn();
const mockModuleAttachments = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    uploadAttachment: (f: File) => mockUpload(f),
    sessions: () => mockSessions(),
    modules: () => mockModules(),
    moduleAttachments: (n: string) => mockModuleAttachments(n),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockUpload.mockReset();
  mockSessions.mockReset().mockResolvedValue([]);
  mockModules.mockReset().mockResolvedValue([]);
  mockModuleAttachments.mockReset().mockResolvedValue([]);
});

describe("AttachmentPill", () => {
  it("renders the title and removes on click", () => {
    const onRemove = vi.fn();
    render(
      <AttachmentPill
        attachment={{ att_id: "a1", source: "file", kind: "", title: "notes.txt" }}
        onRemove={onRemove}
      />,
    );
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Remove/ }));
    expect(onRemove).toHaveBeenCalled();
  });
});

describe("AttachButton", () => {
  it("opens the attach sheet with the source options", () => {
    render(<AttachButton onAttach={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Attach context" }));
    expect(screen.getByRole("button", { name: "Upload a file" })).toBeInTheDocument();
    expect(screen.getByText("Another chat")).toBeInTheDocument();
  });

  it("uploads a file and reports it as a file attachment", async () => {
    mockUpload.mockResolvedValue({ att_id: "a1", title: "notes.txt", kind: "text/plain" });
    const onAttach = vi.fn();
    render(<AttachButton onAttach={onAttach} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Attach context" }));

    const input = screen.getByLabelText("Upload a file");
    const file = new File(["buy milk"], "notes.txt", { type: "text/plain" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(onAttach).toHaveBeenCalledWith(
        expect.objectContaining({ att_id: "a1", source: "file", title: "notes.txt" }),
      ),
    );
  });

  it("offers prior conversations to reference", async () => {
    mockSessions.mockResolvedValue([
      { id: "s9", title: "Trip planning", message_count: 4, last_at: new Date() },
    ]);
    const onAttach = vi.fn();
    render(<AttachButton onAttach={onAttach} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Attach context" }));

    const row = await screen.findByRole("button", { name: /Trip planning/ });
    fireEvent.click(row);
    expect(onAttach).toHaveBeenCalledWith(
      expect.objectContaining({ source: "chat", ref_id: "s9", title: "Trip planning" }),
    );
  });
});
