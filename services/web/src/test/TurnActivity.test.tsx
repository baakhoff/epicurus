import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProcessTimeline, ReadinessBar, ThinkingIndicator } from "@/components/TurnActivity";
import type { Readiness } from "@/lib/contracts";
import type { ToolRun } from "@/stores/chat";

const RUNS: ToolRun[] = [
  { tool: "calendar.list_events", status: "ok" },
  { tool: "knowledge_search", status: "running", detail: '{"q":"abundance"}' },
];

describe("ProcessTimeline (#121)", () => {
  it("renders a humanized step per tool with its status", () => {
    render(<ProcessTimeline runs={RUNS} />);
    expect(screen.getByText("Reading calendar")).toBeInTheDocument();
    expect(screen.getByText("Searching knowledge")).toBeInTheDocument();
    // A running step surfaces in the header.
    expect(screen.getByRole("button", { name: /working/i })).toBeInTheDocument();
  });

  it("summarizes a finished run by step count", () => {
    render(<ProcessTimeline runs={[{ tool: "a.b", status: "ok" }, { tool: "c_d", status: "ok" }]} />);
    expect(screen.getByRole("button", { name: /2 steps/i })).toBeInTheDocument();
  });

  it("reveals a step's detail when clicked", () => {
    render(<ProcessTimeline runs={RUNS} />);
    expect(screen.queryByText(/abundance/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Searching knowledge/ }));
    expect(screen.getByText(/abundance/)).toBeInTheDocument();
  });

  it("starts collapsed when the answer has begun, and the reader can reopen it", () => {
    render(<ProcessTimeline runs={RUNS} collapsed />);
    expect(screen.queryByText("Reading calendar")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /working/i }));
    expect(screen.getByText("Reading calendar")).toBeInTheDocument();
  });

  it("shows the thinking block alongside the steps (ADR-0041)", () => {
    render(<ProcessTimeline runs={RUNS} thinking="weighing the options" />);
    expect(screen.getByText("Thinking")).toBeInTheDocument();
    expect(screen.getByText("weighing the options")).toBeInTheDocument();
  });

  it("renders with thinking only and no tool steps", () => {
    render(<ProcessTimeline runs={[]} thinking="just pondering" />);
    expect(screen.getByRole("button", { name: /thought process/i })).toBeInTheDocument();
    expect(screen.getByText("just pondering")).toBeInTheDocument();
  });

  it("hides the thinking text once the reader collapses it", () => {
    render(<ProcessTimeline runs={[]} thinking="secret reasoning" />);
    expect(screen.getByText("secret reasoning")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Thinking$/ }));
    expect(screen.queryByText("secret reasoning")).not.toBeInTheDocument();
  });

  it("renders nothing without steps or thinking", () => {
    const { container } = render(<ProcessTimeline runs={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ReadinessBar (#122)", () => {
  const readiness: Readiness = {
    ready: false,
    power: "idle",
    components: [
      { name: "modules", ready: true, detail: "2/2 healthy" },
      { name: "model", ready: false, detail: "llama3.2 · warming" },
    ],
  };

  it("shows the warming summary, a determinate bar, and component pills", () => {
    render(<ReadinessBar readiness={readiness} />);
    expect(screen.getByText("Warming the model")).toBeInTheDocument();
    expect(screen.getByText("2/2 healthy")).toBeInTheDocument();
    expect(screen.getByText("llama3.2 · warming")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  });
});

describe("ThinkingIndicator (#121)", () => {
  it("renders the thinking cue", () => {
    render(<ThinkingIndicator />);
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
  });
});
