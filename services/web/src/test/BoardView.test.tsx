import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BoardView } from "@/components/archetypes/BoardView";
import { monthOf, stepMonth, ymd } from "@/components/archetypes/boardViews";

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

// BoardView reads/writes `?view=` for the representation deep-link (#767), so it needs a
// Router context; MemoryRouter keeps the URL in memory.
function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

/** A wrapper whose router starts at *path* — for `?view=` deep-link precedence tests. */
function wrapperAt(path: string) {
  return function routedWrapper({ children }: { children: ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return (
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  };
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
  localStorage.clear(); // the per-page view choice persists (#767) — isolate tests
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

  it("groups view controls and page actions into separate toolbar clusters (#634)", async () => {
    // "Group by"/"Show" must wrap and reflow as one cohesive unit, distinct from the actions
    // cluster — never splitting a control from its sibling or stranding an action alone.
    mockModulePage.mockResolvedValue({
      ...BOARD,
      controls: [
        { id: "group", label: "Group by", value: "due", options: [{ value: "due", label: "Due date" }] },
        { id: "show", label: "Show", value: "open", options: [{ value: "open", label: "Open" }] },
      ],
    });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });
    await screen.findByText("Buy milk");

    const groupLabel = screen.getByLabelText("Group by").closest("label");
    const showLabel = screen.getByLabelText("Show").closest("label");
    const addButton = await screen.findByRole("button", { name: /add task/i });
    const controlsCluster = groupLabel?.parentElement;

    expect(controlsCluster).toContainElement(showLabel);
    expect(controlsCluster).not.toContainElement(addButton);
  });

  // ── drag-and-drop move (#380) ──────────────────────────────────────────────

  // A list-grouped board: two list columns — each carrying its `list_id`, the module's
  // "this column IS a list" marker the drop matching keys on (#763) — and the card's Edit
  // action carries the `to_list_id` picker (the move action), so dragging a card to
  // another column moves it to that list.
  const LIST_BOARD = {
    title: "Tasks",
    columns: [
      {
        id: "work",
        title: "Work",
        list_id: "L-work",
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
      { id: "personal", title: "Personal", list_id: "L-personal", cards: [] },
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

  it("offers no drag at all when no column is a list — even if titles collide (#763)", async () => {
    // A tags-grouped board: a column may share a *title* with a real list ("Personal"),
    // but without a module-declared `list_id` it is not a drop target — and with no
    // target anywhere, the card must not offer a grab it can only dead-end.
    mockModulePage.mockResolvedValue({
      title: "Tasks",
      columns: [
        {
          id: "personal",
          title: "Personal", // same display title as the L-personal list — not a list
          cards: LIST_BOARD.columns[0].cards,
        },
      ],
      actions: [],
    });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    const card = await screen.findByText("Buy milk");
    expect(card.closest('[draggable="true"]')).toBeNull();
  });
});

// ── representations: Board / List / Calendar (#767) ─────────────────────────────

const TODAY = ymd(new Date());

/** A board declaring the reserved `view` control plus structured card fields (#767). */
function viewBoard(view: string) {
  return {
    title: "Tasks",
    controls: [
      {
        id: "view",
        label: "View",
        value: view,
        options: [
          { value: "board", label: "Board" },
          { value: "list", label: "List" },
          { value: "calendar", label: "Calendar" },
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
    columns: [
      {
        id: "today",
        title: "Today",
        cards: [
          {
            id: "t1",
            title: "Buy milk",
            badges: [],
            due: TODAY,
            priority: "high",
            tags: ["errand"],
            list_title: "Personal",
            actions: [
              { tool: "tasks_complete", label: "Complete", icon: "check", args: { task_id: "t1" } },
            ],
          },
        ],
      },
      {
        id: "upcoming",
        title: "Upcoming",
        cards: [
          {
            id: "t2",
            title: "File taxes",
            badges: [],
            due: "2099-01-15",
            priority: "low",
            tags: [],
            list_title: "Work",
            actions: [],
          },
          // The same card under a second column (multi-membership, e.g. a future tags
          // grouping) — the flat representations must show it exactly once.
          {
            id: "t1",
            title: "Buy milk",
            badges: [],
            due: TODAY,
            priority: "high",
            tags: ["errand"],
            list_title: "Personal",
            actions: [],
          },
        ],
      },
    ],
    actions: [],
  };
}

describe("BoardView representations (#767)", () => {
  it("renders the reserved view control as a segmented switcher, not a select", async () => {
    mockModulePage.mockResolvedValue(viewBoard("board"));
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    await screen.findAllByText("Buy milk");
    const switcher = screen.getByRole("group", { name: "View" });
    expect(within(switcher).getByRole("button", { name: "Board" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(switcher).getByRole("button", { name: "List" })).toBeInTheDocument();
    expect(within(switcher).getByRole("button", { name: "Calendar" })).toBeInTheDocument();
    // The other controls keep their selector rendering; no "View" select exists.
    expect(screen.getByLabelText("Show")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "View" })).toBeNull();
  });

  it("switches to the List view: one deduped row per card, structured columns, refetch with ?view=", async () => {
    mockModulePage.mockResolvedValue(viewBoard("board"));
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    await screen.findAllByText("Buy milk");
    fireEvent.click(screen.getByRole("button", { name: "List" }));

    const table = await screen.findByRole("table");
    // t1 appears under two columns in the payload but exactly once as a row.
    expect(within(table).getAllByText("Buy milk")).toHaveLength(1);
    // Structured fields render as columns: due, priority, list, tag chips.
    expect(within(table).getByText(TODAY)).toBeInTheDocument();
    expect(within(table).getByText("high")).toBeInTheDocument();
    expect(within(table).getByText("Personal")).toBeInTheDocument();
    expect(within(table).getByText("errand")).toBeInTheDocument();
    // The switch re-fetches with the reserved param so the module can echo/adjust controls.
    await waitFor(() =>
      expect(mockModulePage).toHaveBeenCalledWith("tasks", "board", { view: "list" }),
    );
    // The Show filter keeps applying — its selector is still in the toolbar.
    expect(screen.getByLabelText("Show")).toBeInTheDocument();
  });

  it("sorts the List view by column, stably, with a direction toggle", async () => {
    mockModulePage.mockResolvedValue(viewBoard("list"));
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    const table = await screen.findByRole("table");
    const titlesIn = () =>
      within(table)
        .getAllByRole("row")
        .slice(1) // drop the header row
        .map((row) => within(row).getAllByRole("cell")[0].textContent);

    // Default sort: by due ascending — today (t1) before 2099 (t2).
    expect(titlesIn()).toEqual(["Buy milk", "File taxes"]);
    // Toggle the Due header → descending.
    fireEvent.click(within(table).getByRole("button", { name: /due/i }));
    expect(titlesIn()).toEqual(["File taxes", "Buy milk"]);
    // Sort by title ascending.
    fireEvent.click(within(table).getByRole("button", { name: /task/i }));
    expect(titlesIn()).toEqual(["Buy milk", "File taxes"]);
  });

  it("invokes the same card actions from a List row (no new mutation path)", async () => {
    mockModulePage.mockResolvedValue(viewBoard("list"));
    mockInvoke.mockResolvedValue({ result: "{}" });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    const table = await screen.findByRole("table");
    fireEvent.click(within(table).getByRole("button", { name: "Complete" }));
    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith("tasks", "tasks_complete", { task_id: "t1" }),
    );
  });

  it("places dated cards in the Calendar month grid and navigates months", async () => {
    // The 20th of next month: deep enough that the current month's 6-week grid (whose
    // trailing pad reaches at most 14 days into the next month) can never show it, on
    // any real-world date this test runs.
    const nextMonth = stepMonth(monthOf(new Date()), 1);
    const nextMonthDue = `${nextMonth.year}-${String(nextMonth.month).padStart(2, "0")}-20`;
    const board = viewBoard("calendar");
    board.columns[1].cards[0].due = nextMonthDue;
    mockModulePage.mockResolvedValue(board);
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    // t1 is due today → a chip in the current month's grid.
    expect(await screen.findByRole("button", { name: "Buy milk" })).toBeInTheDocument();
    // t2 is due deep in next month → not on this grid until we navigate.
    expect(screen.queryByRole("button", { name: "File taxes" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Next month" }));
    expect(screen.getByRole("button", { name: "File taxes" })).toBeInTheDocument();
  });

  it("never places an undated card on the Calendar grid", async () => {
    const board = viewBoard("calendar");
    board.columns[0].cards[0].due = null as unknown as string; // stray undated card
    mockModulePage.mockResolvedValue(board);
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    await screen.findByRole("button", { name: "Next month" }); // grid rendered
    expect(screen.queryByRole("button", { name: "Buy milk" })).toBeNull();
  });

  it("opens a calendar chip into the ordinary card with its actions", async () => {
    mockModulePage.mockResolvedValue(viewBoard("calendar"));
    mockInvoke.mockResolvedValue({ result: "{}" });
    render(<BoardView module="tasks" pageId="board" />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Buy milk" }));
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByRole("button", { name: "Complete" }));
    await waitFor(() =>
      expect(mockInvoke).toHaveBeenCalledWith("tasks", "tasks_complete", { task_id: "t1" }),
    );
  });

  it("persists the chosen view per page and restores it on the next mount (#743 pattern)", async () => {
    mockModulePage.mockResolvedValue(viewBoard("board"));
    const first = render(<BoardView module="tasks" pageId="board" />, { wrapper });
    await screen.findAllByText("Buy milk");
    fireEvent.click(screen.getByRole("button", { name: "List" }));
    await screen.findByRole("table");
    first.unmount();

    // A fresh mount (new router, no ?view=) restores the stored choice and fetches with it.
    mockModulePage.mockClear();
    render(<BoardView module="tasks" pageId="board" />, { wrapper });
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(mockModulePage).toHaveBeenCalledWith("tasks", "board", { view: "list" });
  });

  it("stores the view per page — another page's board is unaffected", async () => {
    mockModulePage.mockResolvedValue(viewBoard("board"));
    const first = render(<BoardView module="tasks" pageId="board" />, { wrapper });
    await screen.findAllByText("Buy milk");
    fireEvent.click(screen.getByRole("button", { name: "Calendar" }));
    await screen.findByRole("button", { name: "Next month" });
    first.unmount();

    render(<BoardView module="tasks" pageId="can" />, { wrapper });
    await screen.findAllByText("Buy milk");
    // The can page keeps the module default (board columns) — no month grid.
    expect(screen.queryByRole("button", { name: "Next month" })).toBeNull();
  });

  it("lets a ?view= deep-link win over the stored choice", async () => {
    localStorage.setItem("board-view:tasks/board", "list");
    mockModulePage.mockResolvedValue(viewBoard("calendar"));
    render(<BoardView module="tasks" pageId="board" />, {
      wrapper: wrapperAt("/m/tasks/board?view=calendar"),
    });

    // The URL's calendar wins: month grid, not the stored list.
    expect(await screen.findByRole("button", { name: "Next month" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(mockModulePage).toHaveBeenCalledWith("tasks", "board", { view: "calendar" });
  });

  it("clamps an unknown ?view= to the kanban board", async () => {
    mockModulePage.mockResolvedValue(viewBoard("board"));
    render(<BoardView module="tasks" pageId="board" />, {
      wrapper: wrapperAt("/m/tasks/board?view=hologram"),
    });

    // Kanban columns render (column headers present), no table, no month grid.
    expect(await screen.findByText("Today")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("button", { name: "Next month" })).toBeNull();
  });

  it("keeps the plain kanban rendering for a board that declares no view control", async () => {
    // BOARD (no `view` control) — even a stray ?view= must not switch representations.
    mockModulePage.mockResolvedValue(BOARD);
    render(<BoardView module="tasks" pageId="board" />, {
      wrapper: wrapperAt("/m/tasks/board?view=list"),
    });

    expect(await screen.findByText("Buy milk")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("group", { name: "View" })).toBeNull();
  });
});
