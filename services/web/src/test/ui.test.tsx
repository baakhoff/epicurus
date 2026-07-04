import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  Button,
  Confirm,
  NumberInput,
  Select,
  Sheet,
  Switch,
  TextInput,
  Tooltip,
} from "@/components/ui";

function thumb(sw: HTMLElement): HTMLElement {
  const span = sw.querySelector("span");
  if (!span) throw new Error("switch has no thumb");
  return span as HTMLElement;
}

// size="sm" (#427): a denser toolbar (e.g. the calendar's) can opt a full Button down
// to match its hand-rolled small controls, without affecting the "md" default used
// everywhere else (forms, the board toolbar).
describe("Button", () => {
  it("uses the comfortable size by default and a compact one for size='sm'", () => {
    const { rerender } = render(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" }).className).toContain("text-sm");
    rerender(<Button size="sm">Save</Button>);
    expect(screen.getByRole("button", { name: "Save" }).className).toContain("text-xs");
  });
});

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

// The shared Tooltip (#334): icon-only controls move their label here. The label is always
// in the DOM (just faded) so it stays discoverable to assistive tech and tests.
describe("Tooltip", () => {
  it("renders the trigger and exposes the label via role=tooltip", () => {
    render(
      <Tooltip label="Working…">
        <button>icon</button>
      </Tooltip>,
    );
    expect(screen.getByRole("button", { name: "icon" })).toBeInTheDocument();
    expect(screen.getByRole("tooltip")).toHaveTextContent("Working…");
  });

  it("keeps the label non-interactive so it never steals the trigger's click", () => {
    render(
      <Tooltip label="hi">
        <button>icon</button>
      </Tooltip>,
    );
    expect(screen.getByRole("tooltip").className).toContain("pointer-events-none");
  });
});

// Focus management for the overlay primitives (#487): aria-modal was already declared,
// but the keyboard didn't honor it — focus stayed behind the backdrop, Tab walked the
// page underneath, and closing dropped focus on <body>. These pin the contract.
describe("Sheet focus management", () => {
  it("moves focus onto the dialog on open when no child claims it", () => {
    render(
      <Sheet open onClose={() => {}} title="Pick">
        <button>alpha</button>
      </Sheet>,
    );
    expect(screen.getByRole("dialog", { name: "Pick" })).toHaveFocus();
  });

  // React applies a child's autoFocus at commit, before effects run — the sheet must
  // not steal focus from a search/rename field (that would pop the phone keyboard shut).
  it("yields to a child rendered with autoFocus", () => {
    render(
      <Sheet open onClose={() => {}} title="Search">
        <TextInput aria-label="Query" autoFocus />
      </Sheet>,
    );
    expect(screen.getByRole("textbox", { name: "Query" })).toHaveFocus();
  });

  it("wraps Tab at the edges instead of leaving the dialog", () => {
    render(
      <Sheet open onClose={() => {}} title="Trap">
        <button>alpha</button>
        <button>omega</button>
      </Sheet>,
    );
    // Focusables in DOM order: the header's Close button first, then alpha, omega.
    const omega = screen.getByRole("button", { name: "omega" });
    const close = screen.getByRole("button", { name: "Close" });

    omega.focus();
    fireEvent.keyDown(omega, { key: "Tab" });
    expect(close).toHaveFocus(); // forward from the last wraps to the first

    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(omega).toHaveFocus(); // backward from the first wraps to the last
  });

  it("Shift+Tab from the container itself wraps to the last focusable", () => {
    render(
      <Sheet open onClose={() => {}} title="Fresh">
        <button>only</button>
      </Sheet>,
    );
    const dialog = screen.getByRole("dialog", { name: "Fresh" });
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("button", { name: "only" })).toHaveFocus();
  });

  it("returns focus to the trigger on close", () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>open sheet</button>
          <Sheet open={open} onClose={() => setOpen(false)} title="T">
            <button>inside</button>
          </Sheet>
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "open sheet" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "T" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(trigger).toHaveFocus();
  });
});

describe("Confirm focus & keyboard", () => {
  it("lands initial focus on Cancel — the safe default under a destructive prompt", () => {
    render(
      <Confirm open message="Delete?" danger onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("cancels on Escape", () => {
    const onCancel = vi.fn();
    render(<Confirm open message="Sure?" onConfirm={() => {}} onCancel={onCancel} />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Cancel" }), { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  // A Confirm stacked above an open Sheet (delete-session over the sessions sheet):
  // one Escape must close only the top layer, not both at once.
  it("Escape over an open Sheet closes only the Confirm", () => {
    const onSheetClose = vi.fn();
    const onCancel = vi.fn();
    render(
      <>
        <Sheet open onClose={onSheetClose} title="Under">
          <button>below</button>
        </Sheet>
        <Confirm open message="Sure?" onConfirm={() => {}} onCancel={onCancel} />
      </>,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Cancel" }), { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSheetClose).not.toHaveBeenCalled();
  });

  it("traps Tab between the two actions", () => {
    render(
      <Confirm open message="Move on?" confirmLabel="Proceed" onConfirm={() => {}} onCancel={() => {}} />,
    );
    const proceed = screen.getByRole("button", { name: "Proceed" });
    proceed.focus();
    fireEvent.keyDown(proceed, { key: "Tab" });
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("returns focus to the trigger on cancel", () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>delete thing</button>
          <Confirm
            open={open}
            message="Delete thing?"
            onConfirm={() => {}}
            onCancel={() => setOpen(false)}
          />
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "delete thing" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(trigger).toHaveFocus();
  });
});

// The shared field primitives (#394): every text input / select routes through these so
// none falls back to the browser-default (white-bordered) control. The themed look is the
// `--color-edge` border on `--color-surface-2` — NOT the undefined `border-line`/`bg-surface`
// tokens the off-style Settings fields used before.
describe("TextInput", () => {
  it("applies the themed field base (edge border on surface-2), not a bare control", () => {
    render(<TextInput aria-label="Name" />);
    const input = screen.getByRole("textbox", { name: "Name" });
    expect(input.className).toContain("border-edge");
    expect(input.className).toContain("bg-surface-2");
    expect(input.className).not.toContain("border-line"); // the old undefined token
  });

  it("merges a caller className over the base (e.g. a width override)", () => {
    render(<TextInput aria-label="Narrow" className="w-24" />);
    expect(screen.getByRole("textbox", { name: "Narrow" }).className).toContain("w-24");
  });

  it("forwards a ref to the underlying input", () => {
    let el: HTMLInputElement | null = null;
    render(
      <TextInput
        aria-label="Ref"
        ref={(n) => {
          el = n;
        }}
      />,
    );
    expect(el).toBeInstanceOf(HTMLInputElement);
  });
});

describe("NumberInput", () => {
  it("renders a themed number field (spinbutton)", () => {
    render(<NumberInput aria-label="Cycles" />);
    const input = screen.getByRole("spinbutton", { name: "Cycles" });
    expect(input).toHaveAttribute("type", "number");
    expect(input.className).toContain("border-edge");
    expect(input.className).toContain("bg-surface-2");
  });
});

describe("Select", () => {
  function options() {
    return (
      <>
        <option value="a">Apple</option>
        <option value="b">Pear</option>
      </>
    );
  }

  it("renders a themed select and fires onChange with the chosen value", () => {
    const onChange = vi.fn();
    render(
      <Select aria-label="Fruit" value="a" onChange={onChange}>
        {options()}
      </Select>,
    );
    const select = screen.getByRole("combobox", { name: "Fruit" });
    expect(select.className).toContain("border-edge");
    expect(select.className).toContain("bg-surface-2");
    expect(select.className).toContain("min-w-0"); // can shrink in a narrow sheet (#335)
    fireEvent.change(select, { target: { value: "b" } });
    expect(onChange).toHaveBeenCalled();
  });

  it("uses the comfortable size by default and a compact one for size='sm'", () => {
    const { rerender } = render(<Select aria-label="S">{options()}</Select>);
    expect(screen.getByRole("combobox", { name: "S" }).className).toContain("text-sm");
    rerender(
      <Select aria-label="S" size="sm">
        {options()}
      </Select>,
    );
    expect(screen.getByRole("combobox", { name: "S" }).className).toContain("text-xs");
  });

  it("forwards a caller className (e.g. w-full) onto the control", () => {
    render(
      <Select aria-label="Wide" className="w-full">
        {options()}
      </Select>,
    );
    expect(screen.getByRole("combobox", { name: "Wide" }).className).toContain("w-full");
  });
});
