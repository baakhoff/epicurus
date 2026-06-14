import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { closeFence, Markdown } from "@/components/Markdown";

const PYTHON_BLOCK = "```python\ndef hello():\n    print('hello')\n```";
const BARE_BLOCK = "```\nraw output\n```";
const OPEN_FENCE = "```python\ndef hello():\n    print('hello')";
const INLINE = "Run `npm install` to add deps.";

beforeEach(() => {
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

describe("closeFence", () => {
  it("appends a closing fence when one is open", () => {
    expect(closeFence("```python\ncode")).toBe("```python\ncode\n```");
  });

  it("leaves text unchanged when all fences are closed", () => {
    expect(closeFence(PYTHON_BLOCK)).toBe(PYTHON_BLOCK);
  });

  it("leaves plain text unchanged", () => {
    expect(closeFence("just prose")).toBe("just prose");
  });
});

describe("Markdown — code block", () => {
  it("renders a copy button for a fenced code block", () => {
    render(<Markdown>{PYTHON_BLOCK}</Markdown>);
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
  });

  it("shows the language label when a language is specified", () => {
    render(<Markdown>{PYTHON_BLOCK}</Markdown>);
    expect(screen.getByText("python")).toBeInTheDocument();
  });

  it("omits the language label for a bare fence", () => {
    const { container } = render(<Markdown>{BARE_BLOCK}</Markdown>);
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
    expect(container.querySelector(".ep-code-lang")).toBeNull();
  });

  it("calls clipboard.writeText with the code text when copy is clicked", async () => {
    render(<Markdown>{PYTHON_BLOCK}</Markdown>);
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        "def hello():\n    print('hello')",
      ),
    );
  });

  it("does not render a copy button for inline code", () => {
    render(<Markdown>{INLINE}</Markdown>);
    expect(screen.queryByRole("button", { name: "Copy code" })).toBeNull();
    expect(screen.getByText("npm install")).toBeInTheDocument();
  });

  it("renders a partial streaming fence as a code block via closeFence", () => {
    render(<Markdown>{OPEN_FENCE}</Markdown>);
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
  });
});
