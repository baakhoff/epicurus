import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BoardView } from "@/components/archetypes/BoardView";

const mockModulePage = vi.fn();
const mockModules = vi.fn();
const mockInvoke = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    modules: (...args: unknown[]) => mockModules(...args),
    invokeModuleTool: (...args: unknown[]) => mockInvoke(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** The manifest the shell reads to build form fields for a tool-backed action. */
const MANIFEST = [
  {
    manifest: {
      name: "tasks",
      tools: [
        {
          name: "tasks_complete",
          input_schema: { type: "object", properties: { task_id: { type: "string" } }, required: ["task_id"] },
        },
        {
          name: "tasks_add",
          input_schema: {
            type: "object",
            properties: { title: { type: "string" }, notes: { type: "string" }, due: { type: "string" } },
            required: ["title"],
          },
        },
        {
          name: "tasks_update",
          input_schema: {
            type: "object",
            properties: {
              task_id: { type: "string" },
              title: { type: "string" },
              notes: { type: "string" },
              due: { type: "string" },
            },
            required: ["task_id"],
          },
        },
      ],
    },
    status: { healthy: true },
  },
];

const BOARD = {
  title: "Tasks",
  columns: [
    {
      id: "today",
      title: "Today",
      cards: [
        {
          id: "t1",
          title: "Buy milk",
          subtitle: "2 litres",
          badges: [{ label: "2026-06-14", tone: "accent" }],
          actions: [
            { tool: "tasks_complete", label: "Complete", icon: "check", args: { task_id: "t1" } },
            {
              tool: "tasks_update",
              label: "Edit",
              icon: "pencil",
              form: true,
              fields: ["title", "notes", "due"],
              args: { task_id: "t1" },
              form_values: { title: "Buy milk", notes: "2 litres", due: "" },
            },
          ],
        },
      ],
    },
  ],
  actions: [
    { tool: "tasks_add", label: "Add task", intent: "primary", icon: "plus", form: true, fields: ["title", "notes", "due"] },
  ],
};

beforeEach(() => {
  mockModulePage.mockReset();
  mockModules.mockReset();
  mockInvoke.mockReset();
  mockModules.mockResolvedValue(MANIFEST);
});

describe("BoardView", () => {
  it("renders the board's columns, cards and actions through the core proxy", async () => {
    mockModulePage.mockResolvedValue(BOARD);
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    expect(await screen.findByText("Buy milk")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("2 litres")).toBeInTheDocument();
    expect(screen.getByText("2026-06-14")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Complete" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add task" })).toBeInTheDocument();
    // The page is fetched with the (empty) control params; the module's defaults apply.
    expect(mockModulePage).toHaveBeenCalledWith("tasks", "board", {});
  });

  it("shows an empty state when no card has any cards", async () => {
    mockModulePage.mockResolvedValue({ title: "Tasks", columns: [], actions: BOARD.actions });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });
    expect(await screen.findByText(/nothing on the board yet/i)).toBeInTheDocument();
    // the board-level Add action is still offered
    expect(screen.getByRole("button", { name: "Add task" })).toBeInTheDocument();
  });

  it("renders an icon_only board action as a compact button with a tooltip label (#337)", async () => {
    mockModulePage.mockResolvedValue({
      ...BOARD,
      actions: [{ ...BOARD.actions[0], icon_only: true }],
    });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    // Still reachable by its accessible name (the label becomes the aria-label + tooltip).
    const add = await screen.findByRole("button", { name: "Add task" });
    expect(screen.getByRole("tooltip")).toHaveTextContent("Add task");
    // And it still opens the same form.
    fireEvent.click(add);
    expect(await screen.findByRole("dialog", { name: "Add task" })).toBeInTheDocument();
  });

  // The toolbar-level action opts into the same responsive shrink as the calendar's page
  // action (#562) — this asserts the DOM contract (aria-label, tooltip, label text kept in
  // the DOM); the CSS breakpoint itself isn't observable in jsdom (checked live instead).
  it("keeps the toolbar action's accessible name and label available at every width (#562)", async () => {
    mockModulePage.mockResolvedValue(BOARD);
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    const add = await screen.findByRole("button", { name: "Add task" });
    expect(add).toHaveAttribute("aria-label", "Add task");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Add task");
    expect(add).toHaveTextContent("Add task");
  });

  it("invokes a card's one-tap action through the core with its fixed args", async () => {
    mockModulePage.mockResolvedValue(BOARD);
    mockInvoke.mockResolvedValue({ result: "{}" });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Complete" }));
    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith("tasks", "tasks_complete", { task_id: "t1" }),
    );
  });

  it("renders a failed action's error below the full actions row, not between the buttons (#472)", async () => {
    mockModulePage.mockResolvedValue(BOARD);
    mockInvoke.mockRejectedValue(new Error("NetworkError when attempting to fetch resource"));
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    const completeBtn = await screen.findByRole("button", { name: "Complete" });
    const row = completeBtn.closest("div")!;
    fireEvent.click(completeBtn);

    const error = await screen.findByText("NetworkError when attempting to fetch resource");
    // The row still holds only its buttons — the error is not interposed between them.
    expect(within(row).getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(within(row).queryByText(error.textContent!)).toBeNull();
    // It renders as the row's next sibling, i.e. below the full row.
    expect(row.nextElementSibling).toBe(error);
  });

  it("opens a form for a form action and submits it through the tool", async () => {
    mockModulePage.mockResolvedValue(BOARD);
    mockInvoke.mockResolvedValue({ result: "{}" });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Add task" }));
    const dialog = await screen.findByRole("dialog", { name: "Add task" });
    // The form fields come from the tool's schema (the modules query), so wait for them.
    fireEvent.change(await within(dialog).findByLabelText("title *"), { target: { value: "Walk dog" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add task" }));

    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith("tasks", "tasks_add", { title: "Walk dog" }),
    );
  });

  it("prefills the edit form from the card and merges the fixed task_id on submit", async () => {
    mockModulePage.mockResolvedValue(BOARD);
    mockInvoke.mockResolvedValue({ result: "{}" });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit" });
    const title = (await within(dialog).findByLabelText("title")) as HTMLInputElement;
    expect(title.value).toBe("Buy milk"); // prefilled from the card
    fireEvent.change(title, { target: { value: "Buy oat milk" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Edit" }));

    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith("tasks", "tasks_update", {
        task_id: "t1",
        title: "Buy oat milk",
        notes: "2 litres",
      }),
    );
  });

  it("renders module-declared view controls and refetches with the chosen query param", async () => {
    // A board carrying view controls (ADR-0049): the shell renders each as a selector and
    // re-fetches the page with `?<id>=<value>` on change, so regrouping stays module-side.
    mockModulePage.mockResolvedValue({
      ...BOARD,
      controls: [
        {
          id: "group",
          label: "Group by",
          value: "due",
          options: [
            { value: "due", label: "Due date" },
            { value: "status", label: "Status" },
          ],
        },
        {
          id: "show",
          label: "Show",
          value: "open",
          options: [
            { value: "open", label: "Open" },
            { value: "all", label: "All" },
          ],
        },
      ],
    });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    await screen.findByText("Buy milk");
    expect(mockModulePage).toHaveBeenCalledWith("tasks", "board", {});

    const group = screen.getByLabelText("Group by") as HTMLSelectElement;
    expect(group.value).toBe("due"); // driven by the module's declared default
    fireEvent.change(group, { target: { value: "status" } });

    // Changing a control re-fetches the page with that control's id as the query param.
    await waitFor(() =>
      expect(mockModulePage).toHaveBeenCalledWith("tasks", "board", { group: "status" }),
    );
    expect(group.value).toBe("status"); // optimistically reflected while refetching
  });

  // ── drag-and-drop move (#380) ──────────────────────────────────────────────

  // A list-grouped board: two list columns, the card's Edit action carries the `to_list_id`
  // picker (the move action), so dragging a card to another column moves it to that list.
  const LIST_BOARD = {
    title: "Tasks",
    columns: [
      {
        id: "work",
        title: "Work",
        cards: [
          {
            id: "t1",
            title: "Buy milk",
            actions: [
              { tool: "tasks_complete", label: "Complete", args: { task_id: "t1", list_id: "L-work" } },
              {
                tool: "tasks_update",
                label: "Edit",
                form: true,
                fields: ["title", "to_list_id"],
                args: { task_id: "t1", list_id: "L-work" },
                form_values: { title: "Buy milk" },
                field_choices: {
                  to_list_id: [
                    { value: "L-work", label: "Work" },
                    { value: "L-personal", label: "Personal" },
                  ],
                },
              },
            ],
          },
        ],
      },
      { id: "personal", title: "Personal", cards: [] },
    ],
    actions: [],
  };

  const dataTransfer = () => ({ setData: vi.fn(), getData: vi.fn(), effectAllowed: "", dropEffect: "" });

  it("moves a task to another list by drag-and-drop, reusing the move action (#380)", async () => {
    mockModulePage.mockResolvedValue(LIST_BOARD);
    mockInvoke.mockResolvedValue({ result: "{}" });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    const cardEl = (await screen.findByText("Buy milk")).closest('[draggable="true"]');
    expect(cardEl).not.toBeNull();
    const personalCol = screen.getByText("Personal").closest("section")!;

    const dt = dataTransfer();
    fireEvent.dragStart(cardEl as Element, { dataTransfer: dt });
    fireEvent.dragOver(personalCol, { dataTransfer: dt });
    fireEvent.drop(personalCol, { dataTransfer: dt });

    // The drop reuses the existing move tool with the target list's id — no new contract.
    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith("tasks", "tasks_update", {
        task_id: "t1",
        list_id: "L-work",
        to_list_id: "L-personal",
      }),
    );
  });

  it("does nothing when a card is dropped back on its own column (#380)", async () => {
    mockModulePage.mockResolvedValue(LIST_BOARD);
    mockInvoke.mockResolvedValue({ result: "{}" });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    const cardEl = (await screen.findByText("Buy milk")).closest('[draggable="true"]')!;
    const workCol = screen.getByText("Work").closest("section")!;

    const dt = dataTransfer();
    fireEvent.dragStart(cardEl, { dataTransfer: dt });
    fireEvent.drop(workCol, { dataTransfer: dt });

    expect(mockInvoke).not.toHaveBeenCalled();
  });
});
