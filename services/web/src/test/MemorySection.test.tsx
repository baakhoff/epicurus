import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemorySection } from "@/components/MemorySection";

const mockMemory = vi.fn();
const mockForget = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    memory: (...args: unknown[]) => mockMemory(...args),
    forgetMemory: (...args: unknown[]) => mockForget(...args),
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

beforeEach(() => {
  mockMemory.mockReset();
  mockForget.mockReset();
  mockMemory.mockResolvedValue({ items: FACTS, total: 2 });
  mockForget.mockResolvedValue({ forgotten: 1 });
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
