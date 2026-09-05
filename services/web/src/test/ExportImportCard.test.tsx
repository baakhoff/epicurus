/** The Settings Export & import card (#867, #877).
 *
 *  What matters here is the *shape of the interaction*, not the styling: an export polls
 *  until it is ready and only then offers a download; an upload shows a preview and applies
 *  nothing until Apply is pressed; an incompatible archive cannot be applied at all; the
 *  "re-enter these secrets" line reaches the operator at both ends; and — #877 — a job this
 *  tab did not start is picked up from the server's job list, because a reload used to
 *  orphan a running export into an archive nobody was ever offered.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ExportImportCard } from "@/components/ExportImportCard";

const mockStartExport = vi.fn();
const mockExport = vi.fn();
const mockUpload = vi.fn();
const mockApply = vi.fn();
const mockImport = vi.fn();
const mockJobs = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ApiError: actual.ApiError,
    api: {
      portabilityJobs: (...a: unknown[]) => mockJobs(...a),
      startPortabilityExport: (...a: unknown[]) => mockStartExport(...a),
      portabilityExport: (...a: unknown[]) => mockExport(...a),
      portabilityArchiveUrl: (id: string) => `/platform/v1/portability/exports/${id}/archive`,
      uploadPortabilityArchive: (...a: unknown[]) => mockUpload(...a),
      applyPortabilityImport: (...a: unknown[]) => mockApply(...a),
      portabilityImport: (...a: unknown[]) => mockImport(...a),
    },
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const SECRETS = { provider_keys: ["openai"], connected_accounts: ["google"] };

const MANIFEST = {
  format_version: 1,
  tenant: "local",
  created_at: "2026-09-04T09:00:00+00:00",
  core_app_version: "0.118.0",
  epicurus_core_version: "0.36.0",
  components: [],
  exclusions: [{ component: "core_files", reason: "derived — rebuilt by the rescan" }],
  secrets: SECRETS,
};

const RUNNING_EXPORT = {
  id: "job-1",
  status: "running",
  created_at: "2026-09-04T09:00:00+00:00",
  updated_at: "2026-09-04T09:00:00+00:00",
  progress: [
    { name: "conversations", kind: "core", state: "running", count: 0 },
    { name: "mail", kind: "module", state: "skipped", count: 0, reason: "module is unreachable" },
  ],
  manifest: null,
  size_bytes: 0,
  error: null,
};

const READY_EXPORT = {
  ...RUNNING_EXPORT,
  status: "ready",
  progress: [
    { name: "conversations", kind: "core", state: "included", count: 42 },
    { name: "mail", kind: "module", state: "skipped", count: 0, reason: "module is unreachable" },
  ],
  manifest: MANIFEST,
  size_bytes: 2_097_152,
};

const STAGED_IMPORT = {
  id: "imp-1",
  status: "staged",
  created_at: "2026-09-04T09:05:00+00:00",
  updated_at: "2026-09-04T09:05:00+00:00",
  preview: {
    manifest: MANIFEST,
    components: [
      { name: "conversations", kind: "core", records: 42, verdict: "ok", detail: null },
      {
        name: "calendar",
        kind: "module",
        records: 9,
        verdict: "refused",
        detail: "module is not installed, not enabled, or not reachable",
      },
    ],
    compatible: true,
    refusal: null,
  },
  report: null,
  error: null,
};

const DONE_IMPORT = {
  ...STAGED_IMPORT,
  status: "done",
  report: {
    components: [
      { name: "conversations", kind: "core", state: "included", created: 42, updated: 0, skipped: 0, warnings: [] },
      { name: "calendar", kind: "module", state: "skipped", created: 0, updated: 0, skipped: 0, warnings: [], reason: "module is not installed" },
    ],
    files: { written: 3, skipped: 1, conflicts: ["notes/edited.md"] },
    rescan_entries: 12,
    rescan_error: null,
    rescan_forced: true,
    reembed: [{ module: "knowledge", status: "started" }],
    reembed_error: null,
    reenter_secrets: SECRETS,
  },
};

const RUNNING_EXPORT_ROW = {
  id: "job-1",
  kind: "export",
  status: "running",
  created_at: "2026-09-04T09:00:00+00:00",
  updated_at: "2026-09-04T09:00:00+00:00",
  archive_available: false,
  size_bytes: 0,
};

const READY_EXPORT_ROW = {
  ...RUNNING_EXPORT_ROW,
  status: "ready",
  archive_available: true,
  size_bytes: 2_097_152,
};

beforeEach(() => {
  mockStartExport.mockReset();
  mockExport.mockReset();
  mockUpload.mockReset();
  mockApply.mockReset();
  mockImport.mockReset();
  mockJobs.mockReset();
  mockJobs.mockResolvedValue([]);
});

function pickFile(name = "epicurus.tar.gz"): void {
  const input = screen.getByTestId("portability-file") as HTMLInputElement;
  const file = new File(["archive bytes"], name, { type: "application/gzip" });
  fireEvent.change(input, { target: { files: [file] } });
}

describe("ExportImportCard (#867)", () => {
  it("reads the job list on mount and starts nothing", async () => {
    render(<ExportImportCard />, { wrapper });
    // The one request an idle card makes — and the only thing that can find a job this tab
    // did not start. Nothing is *begun* without a press.
    await waitFor(() => expect(mockJobs).toHaveBeenCalled());
    expect(mockStartExport).not.toHaveBeenCalled();
    expect(mockExport).not.toHaveBeenCalled();
    expect(mockImport).not.toHaveBeenCalled();
  });

  it("polls an export and only offers the download once it is ready", async () => {
    mockStartExport.mockResolvedValue(RUNNING_EXPORT);
    mockExport.mockResolvedValueOnce(RUNNING_EXPORT).mockResolvedValue(READY_EXPORT);
    render(<ExportImportCard />, { wrapper });

    fireEvent.click(screen.getByRole("button", { name: /export everything/i }));

    // While running there is progress but no link to a half-written archive.
    expect(await screen.findByText("conversations")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();

    const link = await screen.findByRole("link", { name: /download archive/i }, { timeout: 5000 });
    expect(link).toHaveAttribute("href", "/platform/v1/portability/exports/job-1/archive");
    expect(link).toHaveTextContent("2.0 MB");
  });

  it("names a module the export could not reach, without failing the export", async () => {
    mockStartExport.mockResolvedValue(READY_EXPORT);
    mockExport.mockResolvedValue(READY_EXPORT);
    render(<ExportImportCard />, { wrapper });

    fireEvent.click(screen.getByRole("button", { name: /export everything/i }));

    expect(await screen.findByText(/module is unreachable/)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /download archive/i })).toBeInTheDocument();
  });

  it("tells the operator which secrets the archive does not carry", async () => {
    mockStartExport.mockResolvedValue(READY_EXPORT);
    mockExport.mockResolvedValue(READY_EXPORT);
    render(<ExportImportCard />, { wrapper });

    fireEvent.click(screen.getByRole("button", { name: /export everything/i }));

    expect(await screen.findByText(/re-enter API keys for openai/i)).toBeInTheDocument();
    expect(screen.getByText(/reconnect google/i)).toBeInTheDocument();
  });

  it("previews an uploaded archive and applies nothing until Apply is pressed", async () => {
    mockUpload.mockResolvedValue(STAGED_IMPORT);
    mockApply.mockResolvedValue({ ...STAGED_IMPORT, status: "running" });
    mockImport.mockResolvedValue(DONE_IMPORT);
    render(<ExportImportCard />, { wrapper });

    pickFile();

    expect(await screen.findByText("conversations")).toBeInTheDocument();
    expect(screen.getByText("refused")).toBeInTheDocument();
    expect(screen.getByText(/not installed/)).toBeInTheDocument();
    expect(mockApply).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /apply import/i }));

    await waitFor(() => expect(mockApply).toHaveBeenCalledWith("imp-1"));
    expect(await screen.findByText(/42 new/)).toBeInTheDocument();
    expect(screen.getByText(/3 written/)).toBeInTheDocument();
    expect(screen.getByText(/left alone because they differ here/)).toBeInTheDocument();
    // "force-", not just "re-scanned": the #848 fuse was waived to let a fresh install
    // accept a populated tree, and a waived safety rule has to be visible in the report.
    expect(screen.getByText(/force-re-scanned \(12 entries\)/)).toBeInTheDocument();
  });

  it("shows what an archive deliberately leaves behind", async () => {
    mockUpload.mockResolvedValue(STAGED_IMPORT);
    render(<ExportImportCard />, { wrapper });

    pickFile();

    expect(await screen.findByText(/Not in this archive \(1\)/)).toBeInTheDocument();
    expect(screen.getByText("core_files")).toBeInTheDocument();
  });

  it("refuses to offer Apply for an incompatible archive", async () => {
    mockUpload.mockResolvedValue({
      ...STAGED_IMPORT,
      preview: {
        ...STAGED_IMPORT.preview,
        compatible: false,
        refusal: "archive format version 2 cannot be read by this core (format 1)",
      },
    });
    render(<ExportImportCard />, { wrapper });

    pickFile();

    expect(await screen.findByText(/format version 2 cannot be read/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /apply import/i })).toBeDisabled();
  });

  it("renders the server's own detail when an upload is rejected", async () => {
    const { ApiError } = await import("@/lib/api");
    mockUpload.mockRejectedValue(new ApiError(413, "archive exceeds the 4096-byte upload limit"));
    render(<ExportImportCard />, { wrapper });

    pickFile("huge.tar.gz");

    expect(await screen.findByText(/exceeds the 4096-byte upload limit/)).toBeInTheDocument();
  });

});

describe("ExportImportCard — re-attaching after a reload (#877)", () => {
  it("picks up a running export from the job list and polls it to ready", async () => {
    mockJobs
      .mockResolvedValueOnce([RUNNING_EXPORT_ROW])
      .mockResolvedValue([READY_EXPORT_ROW]);
    mockExport.mockResolvedValueOnce(RUNNING_EXPORT).mockResolvedValue(READY_EXPORT);
    render(<ExportImportCard />, { wrapper });

    // Nothing was pressed: the progress on screen belongs to a job started before the load.
    expect(await screen.findByText("conversations")).toBeInTheDocument();
    await waitFor(() => expect(mockExport).toHaveBeenCalledWith("job-1"));
    expect(mockStartExport).not.toHaveBeenCalled();

    const link = await screen.findByRole("link", { name: /download archive/i }, { timeout: 5000 });
    expect(link).toHaveAttribute("href", "/platform/v1/portability/exports/job-1/archive");
  });

  it("does not offer a download whose archive has been cleaned up", async () => {
    // The failure mode the retention window guarantees: the job row outlives its archive.
    mockJobs.mockResolvedValue([{ ...READY_EXPORT_ROW, archive_available: false }]);
    mockExport.mockResolvedValue(READY_EXPORT);
    render(<ExportImportCard />, { wrapper });

    expect(await screen.findByText(/that archive has been cleaned up/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /download archive/i })).not.toBeInTheDocument();
  });

  it("picks up an import left mid-apply and shows the report it finishes with", async () => {
    mockJobs.mockResolvedValue([
      {
        id: "imp-1",
        kind: "import",
        status: "running",
        created_at: "2026-09-04T09:05:00+00:00",
        updated_at: "2026-09-04T09:05:00+00:00",
        archive_available: false,
        size_bytes: 0,
      },
    ]);
    mockImport
      .mockResolvedValueOnce({ ...STAGED_IMPORT, status: "running" })
      .mockResolvedValue(DONE_IMPORT);
    render(<ExportImportCard />, { wrapper });

    await waitFor(() => expect(mockImport).toHaveBeenCalledWith("imp-1"));
    expect(await screen.findByText(/42 new/, undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it("lists the recent jobs, with a download link only where there is an archive", async () => {
    mockJobs.mockResolvedValue([
      READY_EXPORT_ROW,
      { ...READY_EXPORT_ROW, id: "job-0", archive_available: false },
    ]);
    mockExport.mockResolvedValue(READY_EXPORT);
    render(<ExportImportCard />, { wrapper });

    expect(await screen.findByText(/recent jobs \(2\)/i)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /download \(2\.0 MB\)/i }),
    ).toHaveAttribute("href", "/platform/v1/portability/exports/job-1/archive");
    expect(
      screen.queryByRole("link", { name: /\/exports\/job-0\/archive/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/archive cleaned up/i)).toBeInTheDocument();
  });
});
