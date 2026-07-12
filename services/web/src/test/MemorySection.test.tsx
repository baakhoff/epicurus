import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemorySection } from "@/components/MemorySection";

const mockMemory = vi.fn();
const mockForget = vi.fn();
const mockProfile = vi.fn();
const mockSaveProfile = vi.fn();
const mockClearProfile = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    memory: (...args: unknown[]) => mockMemory(...args),
    forgetMemory: (...args: unknown[]) => mockForget(...args),
    profile: (...args: unknown[]) => mockProfile(...args),
    saveProfile: (...args: unknown[]) => mockSaveProfile(...args),
    clearProfile: (...args: unknown[]) => mockClearProfile(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const FACTS = [
  { id: "f2", text: "Lives in Belgrade", source: "auto", created_at: new Date(), score: null },
  { id: "f1", text: "Prefers metric units", source: "tool", created_at: new Date(), score: null },
];

function profileView(overrides: Record<string, unknown> = {}) {
  return {
    profile: { id: 1, content: "The user lives in Belgrade.", source: "auto", created_at: new Date() },
    source: "auto",
    pinned: false,
    versions: [],
    ...overrides,
  };
}

beforeEach(() => {
  mockMemory.mockReset();
  mockForget.mockReset();
  mockProfile.mockReset();
  mockSaveProfile.mockReset();
  mockClearProfile.mockReset();
  mockMemory.mockResolvedValue({ items: FACTS, total: 2 });
  mockForget.mockResolvedValue({ forgotten: 1 });
  mockProfile.mockResolvedValue(profileView());
  mockSaveProfile.mockResolvedValue(profileView({ source: "edited", pinned: true }));
  mockClearProfile.mockResolvedValue({ cleared: 1 });
});

describe("MemorySection", () => {
  it("lists remembered facts with provenance badges and the total", async () => {
    render(<MemorySection />, { wrapper });
    expect(await screen.findByText("Lives in Belgrade")).toBeInTheDocument();
    expect(screen.getByText("Prefers metric units")).toBeInTheDocument();
    expect(screen.getByText("learned")).toBeInTheDocument(); // auto-extracted fact
    expect(screen.getByText("you asked")).toBeInTheDocument(); // remember-tool fact
    expect(screen.getByText(/2 facts/)).toBeInTheDocument();
  });

  it("runs a debounced search with the typed query", async () => {
    render(<MemorySection />, { wrapper });
    await screen.findByText("Lives in Belgrade");
    fireEvent.change(screen.getByLabelText("Search memory"), { target: { value: "units" } });
    await waitFor(() => expect(mockMemory).toHaveBeenCalledWith("units"));
  });

  it("forgets a single fact by its string id", async () => {
    render(<MemorySection />, { wrapper });
    await screen.findByText("Lives in Belgrade");
    fireEvent.click(screen.getAllByLabelText("Forget this")[0]); // newest = f2
    // react-query's mutate forwards a 2nd context arg; assert just the id.
    await waitFor(() => expect(mockForget.mock.calls[0]?.[0]).toBe("f2"));
  });

  it("shows an empty state when nothing is remembered", async () => {
    mockMemory.mockResolvedValue({ items: [], total: 0 });
    render(<MemorySection />, { wrapper });
    expect(await screen.findByText(/Nothing remembered yet/)).toBeInTheDocument();
  });

  it("names the fact row's hover group instead of leaving it unnamed (#572)", async () => {
    render(<MemorySection />, { wrapper });
    await screen.findByText("Lives in Belgrade");
    const forgetBtn = screen.getAllByLabelText("Forget this")[0];
    const forgetClasses = forgetBtn.className.split(/\s+/);
    expect(forgetClasses).toContain("group-hover/fact:opacity-100");
    expect(forgetClasses).not.toContain("group-hover:opacity-100");

    const row = forgetBtn.parentElement!;
    const rowClasses = row.className.split(/\s+/);
    expect(rowClasses).toContain("group/fact");
    expect(rowClasses).not.toContain("group");
  });
});

describe("StandingProfilePanel (#527)", () => {
  it("shows the synthesized profile with an 'auto' badge", async () => {
    render(<MemorySection />, { wrapper });
    expect(await screen.findByDisplayValue("The user lives in Belgrade.")).toBeInTheDocument();
    expect(screen.getByText("auto")).toBeInTheDocument();
  });

  it("badges an operator-pinned edit distinctly", async () => {
    mockProfile.mockResolvedValue(profileView({ source: "edited", pinned: true }));
    render(<MemorySection />, { wrapper });
    expect(await screen.findByText("your edit")).toBeInTheDocument();
  });

  it("saves an edited profile (pinned)", async () => {
    render(<MemorySection />, { wrapper });
    const box = await screen.findByLabelText("Standing profile");
    fireEvent.change(box, { target: { value: "The user is a night owl." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mockSaveProfile.mock.calls[0]?.[0]).toBe("The user is a night owl."),
    );
  });

  it("clears the profile to resume auto-synthesis", async () => {
    render(<MemorySection />, { wrapper });
    await screen.findByDisplayValue("The user lives in Belgrade.");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(mockClearProfile).toHaveBeenCalled());
  });

  it("shows an empty editor and no badge when nothing is synthesized yet", async () => {
    mockProfile.mockResolvedValue({ profile: null, source: null, pinned: false, versions: [] });
    render(<MemorySection />, { wrapper });
    await screen.findByText("Lives in Belgrade"); // wait for render
    const box = screen.getByLabelText("Standing profile") as HTMLTextAreaElement;
    expect(box.value).toBe("");
    expect(screen.queryByText("auto")).not.toBeInTheDocument();
    // no synthesized profile → the Clear affordance is hidden
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });
});
