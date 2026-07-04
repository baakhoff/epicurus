import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Toaster } from "@/components/Toaster";
import { toast, useToasts } from "@/stores/toasts";

// The themed replacement for window.alert (#488): store-driven cards the shell renders
// bottom-anchored. Each card is a role="status" live region (implicit polite announcement),
// closable by hand, and auto-dismissed on a per-tone clock — errors linger longer than info.
describe("Toaster", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useToasts.setState({ toasts: [] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders toast.error as a danger-toned status card", () => {
    render(<Toaster />);
    act(() => toast.error("Could not delete file: boom"));
    const card = screen.getByRole("status");
    expect(card).toHaveTextContent("Could not delete file: boom");
    expect(card.className).toContain("border-danger");
  });

  it("renders toast.info on the quiet edge tone, not the danger one", () => {
    render(<Toaster />);
    act(() => toast.info("Saved."));
    const card = screen.getByRole("status");
    expect(card.className).toContain("border-edge");
    expect(card.className).not.toContain("border-danger");
  });

  it("dismisses by hand via the close button", () => {
    render(<Toaster />);
    act(() => toast.info("Saved."));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("auto-dismisses info quickly and lets errors linger longer", () => {
    render(<Toaster />);
    act(() => {
      toast.info("done");
      toast.error("failed");
    });
    expect(screen.getAllByRole("status")).toHaveLength(2);
    act(() => vi.advanceTimersByTime(4000)); // the info lifetime
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("failed");
    act(() => vi.advanceTimersByTime(4000)); // 8s total — the error lifetime
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  // A retried mutation re-raising the same failure must not fill the screen with copies.
  it("re-raising an identical message replaces the card instead of stacking", () => {
    render(<Toaster />);
    act(() => {
      toast.error("same failure");
      toast.error("same failure");
    });
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("a replacement restarts the auto-dismiss clock", () => {
    render(<Toaster />);
    act(() => toast.error("flaky"));
    act(() => vi.advanceTimersByTime(6000));
    act(() => toast.error("flaky")); // re-raised at t=6s → fresh card, fresh clock
    act(() => vi.advanceTimersByTime(4000)); // t=10s: the original clock would have expired
    expect(screen.getByRole("status")).toHaveTextContent("flaky");
    act(() => vi.advanceTimersByTime(4000)); // a full 8s after the re-raise
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  // Same message, different tone = two distinct cards (an info echo of an error text
  // must not swallow the error).
  it("keeps identical text apart when the tones differ", () => {
    render(<Toaster />);
    act(() => {
      toast.error("ambiguous");
      toast.info("ambiguous");
    });
    expect(screen.getAllByRole("status")).toHaveLength(2);
  });
});
