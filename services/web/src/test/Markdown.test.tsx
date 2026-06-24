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

describe("Markdown — block structure", () => {
  it("renders ATX headings as heading elements", () => {
    const { container } = render(<Markdown>{"# Title\n\n## Subtitle\n\n### Section"}</Markdown>);
    expect(container.querySelector("h1")?.textContent).toBe("Title");
    expect(container.querySelector("h2")?.textContent).toBe("Subtitle");
    expect(container.querySelector("h3")?.textContent).toBe("Section");
  });

  it("renders a dash list as a <ul> with one <li> per item", () => {
    const { container } = render(<Markdown>{"- first\n- second\n- third"}</Markdown>);
    const items = container.querySelectorAll("ul > li");
    expect(items).toHaveLength(3);
    expect(items[0].textContent).toBe("first");
    expect(items[2].textContent).toBe("third");
  });

  it("renders a numbered list as an <ol> with one <li> per item", () => {
    const { container } = render(<Markdown>{"1. one\n2. two"}</Markdown>);
    const items = container.querySelectorAll("ol > li");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toBe("one");
    expect(items[1].textContent).toBe("two");
  });

  it("renders nested lists as nested <ul> elements", () => {
    const { container } = render(<Markdown>{"- parent\n  - child"}</Markdown>);
    expect(container.querySelector("ul ul > li")?.textContent).toBe("child");
  });

  it("renders GFM task-list items with a checkbox", () => {
    const { container } = render(<Markdown>{"- [x] done\n- [ ] todo"}</Markdown>);
    const boxes = container.querySelectorAll<HTMLInputElement>('li.task-list-item input[type="checkbox"]');
    expect(boxes).toHaveLength(2);
    expect(boxes[0].checked).toBe(true);
    expect(boxes[1].checked).toBe(false);
  });

  it("renders bold and italic emphasis as <strong> and <em>", () => {
    const { container } = render(<Markdown>{"**bold** and *italic*"}</Markdown>);
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelector("em")?.textContent).toBe("italic");
  });
});
