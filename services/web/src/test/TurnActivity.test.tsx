import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProcessTimeline, ReadinessBar, ThinkingIndicator } from "@/components/TurnActivity";
import type { Readiness } from "@/lib/contracts";
import type { ActivityItem } from "@/stores/chat";

const ITEMS: ActivityItem[] = [
  { kind: "tool", run: { tool: "calendar.list_events", status: "ok" } },
  { kind: "tool", run: { tool: "knowledge_search", status: "running", detail: '{"q":"abundance"}' } },
];

describe("ProcessTimeline (#121)", () => {
  it("renders a humanized step per tool with its status", () => {
    render(<ProcessTimeline items={ITEMS} />);
    expect(screen.getByText("Reading calendar")).toBeInTheDocument();
    expect(screen.getByText("Searching knowledge")).toBeInTheDocument();
    // A running step surfaces in the header.
    expect(screen.getByRole("button", { name: /working/i })).toBeInTheDocument();
  });

  it("summarizes a finished run by step count", () => {
    render(
      <ProcessTimeline
        items={[
          { kind: "tool", run: { tool: "a.b", status: "ok" } },
          { kind: "tool", run: { tool: "c_d", status: "ok" } },
        ]}
      />,
    );
    expect(screen.getByRole("button", { name: /2 steps/i })).toBeInTheDocument();
  });

  it("reveals a step's detail when clicked", () => {
    render(<ProcessTimeline items={ITEMS} />);
    expect(screen.queryByText(/abundance/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Searching knowledge/ }));
    expect(screen.getByText(/abundance/)).toBeInTheDocument();
  });

  it("starts collapsed when the answer has begun, and the reader can reopen it", () => {
    render(<ProcessTimeline items={ITEMS} collapsed />);
    expect(screen.queryByText("Reading calendar")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /working/i }));
    expect(screen.getByText("Reading calendar")).toBeInTheDocument();
  });

  it("shows a thinking block alongside the steps (ADR-0041)", () => {
    render(
      <ProcessTimeline items={[{ kind: "thinking", text: "weighing the options" }, ...ITEMS]} />,
    );
    expect(screen.getByText("Thinking")).toBeInTheDocument();
    expect(screen.getByText("weighing the options")).toBeInTheDocument();
  });

  it("renders with thinking only and no tool steps", () => {
    render(<ProcessTimeline items={[{ kind: "thinking", text: "just pondering" }]} />);
    expect(screen.getByRole("button", { name: /thought process/i })).toBeInTheDocument();
    expect(screen.getByText("just pondering")).toBeInTheDocument();
  });

  it("hides the thinking text once the reader collapses it", () => {
    render(<ProcessTimeline items={[{ kind: "thinking", text: "secret reasoning" }]} />);
    expect(screen.getByText("secret reasoning")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Thinking$/ }));
    expect(screen.queryByText("secret reasoning")).not.toBeInTheDocument();
  });

  it("renders nothing for an empty timeline", () => {
    const { container } = render(<ProcessTimeline items={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders thinking and tools in chronological order (think → call → think, #300)", () => {
    const items: ActivityItem[] = [
      { kind: "thinking", text: "first I plan" },
      { kind: "tool", run: { tool: "knowledge_search", status: "ok" } },
      { kind: "thinking", text: "then I refine" },
    ];
    render(<ProcessTimeline items={items} />);
    const plan = screen.getByText("first I plan");
    const tool = screen.getByText("Searching knowledge");
    const refine = screen.getByText("then I refine");
    // DOM order matches the timeline order: plan, then the tool, then refine.
    expect(plan.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tool.compareDocumentPosition(refine) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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
