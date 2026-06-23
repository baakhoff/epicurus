import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryScreen } from "@/screens/MemoryScreen";

const mockMemory = vi.fn();
const mockForget = vi.fn();
const mockSessions = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    memory: (...args: unknown[]) => mockMemory(...args),
    forgetMemory: (...args: unknown[]) => mockForget(...args),
    sessions: (...args: unknown[]) => mockSessions(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const ITEMS = [
  { id: 2, session_id: "s1", role: "assistant", text: "hi there", created_at: new Date(), score: null },
  { id: 1, session_id: "s1", role: "user", text: "hello world", created_at: new Date(), score: null },
];

beforeEach(() => {
  mockMemory.mockReset();
  mockForget.mockReset();
  mockSessions.mockReset();
  mockMemory.mockResolvedValue({ items: ITEMS, total: 2 });
  mockForget.mockResolvedValue({ forgotten: 1 });
  mockSessions.mockResolvedValue([
    { id: "s1", title: "Garden talk", message_count: 2, last_at: new Date() },
  ]);
});

describe("MemoryScreen", () => {
  it("lists the recall corpus with role labels, total, and source conversation", async () => {
    render(<MemoryScreen />, { wrapper });
    expect(await screen.findByText("hi there")).toBeInTheDocument();
    expect(screen.getByText("hello world")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument(); // user-turn role badge
    expect(screen.getByText(/2 memories/)).toBeInTheDocument();
    // both snippets link back to their source conversation by title
    expect(screen.getAllByText(/Garden talk/)).toHaveLength(2);
  });

  it("runs a debounced search with the typed query", async () => {
    render(<MemoryScreen />, { wrapper });
    await screen.findByText("hi there");
    fireEvent.change(screen.getByLabelText("Search memory"), { target: { value: "apples" } });
    await waitFor(() => expect(mockMemory).toHaveBeenCalledWith("apples"));
  });

  it("forgets a single memory by id", async () => {
    render(<MemoryScreen />, { wrapper });
    await screen.findByText("hi there");
    fireEvent.click(screen.getAllByLabelText("Forget this memory")[0]); // newest = id 2
    // react-query's mutate forwards a 2nd context arg; assert just the id.
    await waitFor(() => expect(mockForget.mock.calls[0]?.[0]).toBe(2));
  });

  it("shows an empty state when nothing is remembered", async () => {
    mockMemory.mockResolvedValue({ items: [], total: 0 });
    render(<MemoryScreen />, { wrapper });
    expect(await screen.findByText(/Nothing remembered yet/)).toBeInTheDocument();
  });
});
