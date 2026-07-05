import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The palette reuses the shell's own queries — sessions, modules, power (#491).
const mockSessions = vi.fn();
const mockModules = vi.fn();
const mockPower = vi.fn();
const mockSetPower = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    sessions: () => mockSessions(),
    modules: () => mockModules(),
    power: () => mockPower(),
    setPower: (state: string) => mockSetPower(state),
  },
}));

import { CommandPalette } from "@/components/CommandPalette";
import { ModuleSnapshot } from "@/lib/contracts";
import { useChat } from "@/stores/chat";

const notesModule = ModuleSnapshot.parse({
  manifest: {
    name: "notes",
    version: "0.6.0",
    pages: [{ id: "notes", title: "Notes", archetype: "editor", icon: "pencil", nav_order: 30 }],
  },
  status: { healthy: true },
});

const hour = 3_600_000;
const day = 86_400_000;
const at = (msAgo: number) => new Date(Date.now() - msAgo);

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

/** A controlled host, like the Shell: the palette owns its hotkey, the host owns `open`. */
function Harness({ initialOpen = true }: { initialOpen?: boolean }) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <>
      <button onClick={() => setOpen(true)}>opener</button>
      <CommandPalette open={open} onOpenChange={setOpen} />
      <LocationProbe />
    </>
  );
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/settings"]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockSessions.mockReset().mockResolvedValue([
    { id: "s-old", title: "Ancient history", message_count: 9, last_at: at(45 * day) },
    { id: "s-today", title: "Fresh plans", message_count: 3, last_at: at(2 * hour) },
    { id: "s-yesterday", title: "Balcony lamp", message_count: 5, last_at: at(day + 2 * hour) },
  ]);
  mockModules.mockReset().mockResolvedValue([notesModule]);
  mockPower.mockReset().mockResolvedValue({ state: "idle" });
  mockSetPower.mockReset().mockResolvedValue({ state: "paused" });
  useChat.setState({ sessionId: "current", streaming: false, abort: null, segments: [] });
});

describe("Command palette (#491)", () => {
  it("lists actions, recency-ordered conversations, then pages", async () => {
    render(<Harness />, { wrapper });
    await screen.findByText("Fresh plans");

    const labels = screen.getAllByRole("option").map((o) => o.textContent ?? "");
    // The option's textContent leads with its label (the hint trails), so prefix-match —
    // a bare includes() would let the "New note" action's "Notes" hint shadow the page.
    const order = (label: string) => labels.findIndex((l) => l.startsWith(label));
    // Actions lead; sessions are recency-ordered (not server order); pages follow.
    expect(order("New chat")).toBeLessThan(order("Fresh plans"));
    expect(order("Fresh plans")).toBeLessThan(order("Balcony lamp"));
    expect(order("Balcony lamp")).toBeLessThan(order("Ancient history"));
    expect(order("Ancient history")).toBeLessThan(order("Chat"));
    // The notes module contributes its page, same data the rail renders.
    expect(order("Notes")).toBeGreaterThan(order("Settings"));
  });

  it("fuzzy-filters every section and reports an empty result honestly", async () => {
    render(<Harness />, { wrapper });
    await screen.findByText("Fresh plans");

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "mod" } });
    const labels = screen.getAllByRole("option").map((o) => o.textContent);
    expect(labels.some((l) => l?.includes("Models"))).toBe(true);
    expect(labels.some((l) => l?.includes("Modules"))).toBe(true);
    expect(labels.some((l) => l?.includes("Fresh plans"))).toBe(false);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "zzzz" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText(/Nothing matches/)).toBeInTheDocument();
  });

  it("drives the cursor with arrows and opens the picked conversation on Enter", async () => {
    render(<Harness />, { wrapper });
    await screen.findByText("Fresh plans");
    const input = screen.getByRole("combobox");

    // Combobox semantics: focus stays in the input; the active option is state.
    expect(input).toHaveAttribute("aria-activedescendant", "palette-option-0");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input).toHaveAttribute("aria-activedescendant", "palette-option-1");

    // Three actions (New chat, Pause, New note) sit above the sessions.
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(useChat.getState().sessionId).toBe("s-today");
    expect(screen.getByTestId("location").textContent).toBe("/");
    expect(screen.queryByRole("dialog")).toBeNull(); // closed after running
  });

  it("navigates to a page picked by search", async () => {
    render(<Harness />, { wrapper });
    await screen.findByText("Fresh plans");

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "files" } });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(screen.getByTestId("location").textContent).toBe("/files");
  });

  it("starts a fresh conversation from the New-chat action", async () => {
    render(<Harness />, { wrapper });
    await screen.findByText("Fresh plans");
    const before = useChat.getState().sessionId;

    fireEvent.click(screen.getByText("New chat"));
    expect(useChat.getState().sessionId).not.toBe(before);
    expect(screen.getByTestId("location").textContent).toBe("/");
  });

  it("offers New note only when a notes editor page exists, deep-linking ?new=1", async () => {
    render(<Harness />, { wrapper });
    await screen.findByText("Fresh plans");
    fireEvent.click(screen.getByText("New note"));
    expect(screen.getByTestId("location").textContent).toBe("/m/notes/notes?new=1");
  });

  it("hides New note when notes is not installed", async () => {
    mockModules.mockResolvedValue([]);
    render(<Harness />, { wrapper });
    await screen.findByText("Fresh plans");
    expect(screen.queryByText("New note")).toBeNull();
  });

  it("mirrors the power state: paused offers Wake up and requests idle", async () => {
    mockPower.mockResolvedValue({ state: "paused" });
    render(<Harness />, { wrapper });
    await screen.findByText("Wake up");

    fireEvent.click(screen.getByText("Wake up"));
    await waitFor(() => expect(mockSetPower).toHaveBeenCalledWith("idle"));
  });

  it("toggles with Ctrl/Cmd+K from anywhere — the listener is alive while closed", async () => {
    render(<Harness initialOpen={false} />, { wrapper });
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "K", metaKey: true }); // either modifier, either case
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes on Escape and hands focus back to the opener (#487)", async () => {
    render(<Harness initialOpen={false} />, { wrapper });
    const opener = screen.getByText("opener");
    opener.focus();
    fireEvent.click(opener);
    await screen.findByRole("dialog");
    // The input claimed focus on open.
    expect(document.activeElement).toBe(screen.getByRole("combobox"));

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(opener);
  });
});
