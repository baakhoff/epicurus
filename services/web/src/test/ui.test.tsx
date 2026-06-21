import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Switch } from "@/components/ui";

function thumb(sw: HTMLElement): HTMLElement {
  const span = sw.querySelector("span");
  if (!span) throw new Error("switch has no thumb");
  return span as HTMLElement;
}

describe("Switch", () => {
  it("exposes the switch role with the label as its accessible name", () => {
    render(<Switch checked={false} onChange={() => {}} label="Toggle knowledge_search" />);
    const sw = screen.getByRole("switch", { name: "Toggle knowledge_search" });
    expect(sw).toHaveAttribute("aria-checked", "false");
  });

  it("reflects the checked state on aria-checked", () => {
    render(<Switch checked onChange={() => {}} label="x" />);
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("calls onChange with the negated value when clicked", () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} label="x" />);
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("does not fire onChange while disabled", () => {
    const onChange = vi.fn();
    render(<Switch checked onChange={onChange} disabled label="x" />);
    const sw = screen.getByRole("switch");
    expect(sw).toBeDisabled();
    fireEvent.click(sw);
    expect(onChange).not.toHaveBeenCalled();
  });

  // The affordance fix (#245): the thumb is a constant, bright, raised circle in BOTH
  // states — only the track colour and the thumb's position change. It must never revert
  // to the old dark `bg-canvas` thumb that read as a hole escaping the pill.
  it("keeps a constant bright thumb and lets the track carry on/off", () => {
    const { rerender } = render(<Switch checked onChange={() => {}} label="x" />);
    let sw = screen.getByRole("switch");
    expect(sw.className).toContain("bg-accent"); // track on
    expect(thumb(sw).className).toContain("bg-ink"); // bright thumb
    expect(thumb(sw).className).toContain("translate-x-5"); // slid to the "on" end
    expect(thumb(sw).className).not.toContain("bg-canvas"); // never the old dark hole

    rerender(<Switch checked={false} onChange={() => {}} label="x" />);
    sw = screen.getByRole("switch");
    expect(sw.className).toContain("bg-edge-strong"); // track off
    expect(thumb(sw).className).toContain("bg-ink"); // same bright thumb
    expect(thumb(sw).className).toContain("translate-x-0"); // slid to the "off" end
  });

  // Regression guard (#245): the thumb must be absolutely positioned with an explicit
  // resting edge — NOT laid out by flex on the <button>. Firefox ignores `display:flex`
  // on a button, and an absolute child with no `left` resolves its static position
  // differently across engines; either way the dot landed on the wrong side in Firefox.
  it("positions the thumb absolutely with an explicit edge (Firefox-safe), not via flex", () => {
    render(<Switch checked={false} onChange={() => {}} label="x" />);
    const sw = screen.getByRole("switch");
    expect(sw.className).not.toContain("flex"); // no inline-flex/flex on the button
    const t = thumb(sw);
    expect(t.className).toContain("absolute");
    expect(t.className).toContain("left-0"); // explicit resting edge, engine-agnostic
  });
});
