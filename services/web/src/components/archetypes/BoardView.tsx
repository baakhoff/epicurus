/**
 * The `board` archetype (ADR-0018): columns of cards, core-rendered. The module
 * supplies only data — columns, cards, declarative *actions*, and *view controls* —
 * through the core page proxy; this screen renders it in ε style. No module markup runs here.
 *
 * Unlike `browser`, a board mutates: each action names one of the module's MCP
 * tools, which the shell invokes through the core (`invokeModuleTool`, validated
 * against the manifest). A `form` action collects arguments via the shared
 * SchemaForm first; a `confirm` action gates a one-tap call behind a dialog. After
 * any successful call the page data is refetched, so the board reflects the change.
 *
 * Drag-and-drop (#380): a card can be **dragged between columns** to move the task, reusing
 * the *existing* move action (no new contract) — see the drag-and-drop note below. The
 * action/form path stays as the accessible, pointer-free fallback.
 *
 * View controls (ADR-0049) are module-declared selectors — e.g. group-by and filters —
 * rendered in the toolbar. Changing one updates a query-param map and re-fetches the page,
 * so regrouping/filtering happens module-side (the board carries no task fields here). The
 * selected values live in this component (like the calendar's view/cursor), so a control is
 * driven optimistically while the refetch is in flight.
 *
 * Representations (#767): a board page may declare the **reserved `view` control**
 * (Board / List / Calendar) — three client-side renderings of the same payload. The shell
 * renders it as the standard segmented view switcher and switches the body: kanban columns
 * (default), a sortable flat **list**, or a **month grid** placing cards by their
 * structured `due` field. The choice persists per page (localStorage, the #743 pattern);
 * a `?view=` URL param wins over the stored choice and is kept up to date on switch, so
 * the current view is always deep-linkable. The param rides the normal control re-fetch,
 * letting the module echo it and adjust its other controls (tasks hides *Group by* off
 * the Board view — grouping shapes kanban columns only).
 *
 * Columns scroll horizontally (kanban-style) on every width.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Badge, EmptyState, Select, Sheet, Spinner, cn } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { toast } from "@/stores/toasts";
import {
  BoardData,
  type BoardAction,
  type BoardCard,
  type BoardColumn,
  type BoardControl,
} from "@/lib/contracts";

import { ActionControl } from "./ActionControl";
import {
  BOARD_VIEW,
  CALENDAR_VIEW,
  LIST_VIEW,
  VIEW_CONTROL_ID,
  cardsByDate,
  dueTone,
  flattenCards,
  monthCells,
  monthLabel,
  monthOf,
  sortCards,
  stepMonth,
  ymd,
  type DueTone,
  type ListSort,
  type ListSortKey,
  type MonthCursor,
} from "./boardViews";

/* ── drag-and-drop move (#380) ───────────────────────────────────────────────
 * Dropping a card on another column moves the task by reusing the *existing* move action
 * (`tasks_update` with `to_list_id`, #257) — no new backend contract. A card's move action is
 * the one whose `field_choices.to_list_id` lists the writable lists as `{value: list_id, label:
 * list_title}`. A list-grouped column's title *is* the list title, so the drop target is the
 * choice whose label matches the column's title. Columns that aren't lists (grouped by due /
 * status / priority) match nothing, so a drop there is a no-op — the move action can only change
 * the list, not those dimensions.
 */

/** The card's move action — the one carrying a `to_list_id` list picker — or undefined. */
function moveActionOf(card: BoardCard): BoardAction | undefined {
  return card.actions.find((a) => (a.field_choices?.to_list_id?.length ?? 0) > 0);
}

/** The `to_list_id` value for dropping a card on the column titled *columnTitle*, or undefined
 *  when that column isn't one of the move action's target lists. */
function moveTargetFor(action: BoardAction, columnTitle: string): string | undefined {
  return action.field_choices?.to_list_id?.find((c) => c.label === columnTitle)?.value;
}

/* ── view persistence (#767, the #743 localStorage pattern) ──────────────────── */

const viewStorageKey = (module: string, pageId: string): string =>
  `board-view:${module}/${pageId}`;

/** The operator's last-chosen representation for this page, else null (module default). */
function readStoredView(module: string, pageId: string): string | null {
  try {
    return localStorage.getItem(viewStorageKey(module, pageId));
  } catch {
    return null;
  }
}

function writeStoredView(module: string, pageId: string, view: string): void {
  try {
    localStorage.setItem(viewStorageKey(module, pageId), view);
  } catch {
    /* storage full / unavailable — persistence is best-effort */
  }
}

/** One module-declared view control (ADR-0049), rendered as a labeled selector. */
function ControlSelect({
  control,
  value,
  onChange,
}: {
  control: BoardControl;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs text-ink-faint">
      <span className="whitespace-nowrap">{control.label}</span>
      <Select
        size="sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="transition-colors hover:border-edge-strong"
      >
        {control.options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </Select>
    </label>
  );
}

/** The reserved `view` control (#767), rendered as the shell's standard segmented view
 *  switcher — the exact affordance the calendar page uses for Month / Week / Agenda — so
 *  "three representations, switchable in place" reads the same on every page. */
function ViewSwitcher({
  control,
  value,
  onChange,
}: {
  control: BoardControl;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div
      role="group"
      aria-label={control.label}
      className="flex rounded-(--radius-field) border border-edge p-0.5"
    >
      {control.options.map((option) => (
        <button
          key={option.value}
          onClick={() => onChange(option.value)}
          aria-pressed={option.value === value}
          className={cn(
            "rounded-[calc(var(--radius-field)-2px)] px-2.5 py-1 text-xs transition-colors",
            option.value === value ? "bg-accent-dim text-accent-strong" : "text-ink-dim hover:text-ink",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function BoardCardView({
  module,
  pageId,
  card,
  draggable,
  dragging,
  onDragStart,
  onDragEnd,
}: {
  module: string;
  pageId: string;
  card: BoardCard;
  /** Whether this card can be dragged to another list (it has a move action). */
  draggable: boolean;
  /** This card is the one currently being dragged (dim it). */
  dragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
}) {
  // One combined slot for whichever action last failed, rendered below the full
  // actions row rather than per-action inline (#472) — cleared on the next success.
  const [actionError, setActionError] = useState<string | null>(null);
  return (
    <div
      draggable={draggable}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = "move";
        // Some browsers require data to be set for a drag to start.
        e.dataTransfer.setData("text/plain", card.id);
        onDragStart();
      }}
      onDragEnd={onDragEnd}
      className={cn(
        "rounded-(--radius-card) border border-edge bg-surface p-3",
        draggable && "cursor-grab active:cursor-grabbing",
        dragging && "opacity-40",
      )}
    >
      <p className={cn("text-sm leading-snug text-ink", card.done && "text-ink-faint line-through")}>
        {card.title}
      </p>
      {card.subtitle && <p className="mt-0.5 truncate text-xs text-ink-faint">{card.subtitle}</p>}
      {card.badges.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {card.badges.map((badge, i) => (
            <Badge key={`${badge.label}-${i}`} tone={badge.tone}>
              {badge.label}
            </Badge>
          ))}
        </div>
      )}
      {card.actions.length > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1 border-t border-edge pt-2">
          {card.actions.map((action) => (
            <ActionControl
              key={action.tool + action.label}
              module={module}
              pageId={pageId}
              action={action}
              compact
              onSuccess={() => setActionError(null)}
              onError={setActionError}
            />
          ))}
        </div>
      )}
      {actionError && <p className="mt-1.5 text-[11px] text-danger">{actionError}</p>}
    </div>
  );
}

/* ── the List representation (#767) ──────────────────────────────────────────── */

/** Due-cell text tone, matching the board's due-badge tone language. */
const DUE_TEXT_TONE: Record<DueTone, string> = {
  overdue: "text-danger",
  today: "text-accent-strong",
  upcoming: "text-ink-dim",
  done: "text-ink-faint",
};

function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
  className,
}: {
  label: string;
  sortKey: ListSortKey;
  sort: ListSort;
  onSort: (key: ListSortKey) => void;
  className?: string;
}) {
  const active = sort.key === sortKey;
  return (
    <th
      aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : undefined}
      className={cn("px-3 py-2 text-left font-medium", className)}
    >
      <button
        onClick={() => onSort(sortKey)}
        className={cn(
          "inline-flex items-center gap-1 uppercase tracking-wide transition-colors hover:text-ink",
          active ? "text-ink" : "text-ink-faint",
        )}
      >
        {label}
        {active && <span aria-hidden>{sort.dir === 1 ? "↑" : "↓"}</span>}
      </button>
    </th>
  );
}

function ListRow({
  module,
  pageId,
  card,
  today,
  showPriority,
  showList,
  showTags,
}: {
  module: string;
  pageId: string;
  card: BoardCard;
  today: string;
  showPriority: boolean;
  showList: boolean;
  showTags: boolean;
}) {
  const [actionError, setActionError] = useState<string | null>(null);
  const tone = dueTone(card, today);
  return (
    <tr className="border-b border-edge align-top last:border-b-0 hover:bg-surface-2/50">
      <td className="px-3 py-2.5">
        <p className={cn("text-sm text-ink", card.done && "text-ink-faint line-through")}>
          {card.title}
        </p>
        {card.subtitle && <p className="mt-0.5 text-xs text-ink-faint">{card.subtitle}</p>}
      </td>
      <td className={cn("whitespace-nowrap px-3 py-2.5 text-xs tabular-nums", DUE_TEXT_TONE[tone])}>
        {card.due ? card.due.slice(0, 10) : "—"}
      </td>
      {showPriority && (
        <td className="whitespace-nowrap px-3 py-2.5 text-xs text-ink-dim">
          {card.priority ?? "—"}
        </td>
      )}
      {showList && (
        <td className="whitespace-nowrap px-3 py-2.5 text-xs text-ink-dim">
          {card.list_title ?? "—"}
        </td>
      )}
      {showTags && (
        <td className="px-3 py-2.5">
          {card.tags.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {card.tags.map((tag) => (
                <Badge key={tag} tone="accent">
                  {tag}
                </Badge>
              ))}
            </div>
          )}
        </td>
      )}
      <td className="px-3 py-1.5">
        <div className="flex flex-wrap items-center justify-end gap-1">
          {card.actions.map((action) => (
            <ActionControl
              key={action.tool + action.label}
              module={module}
              pageId={pageId}
              action={action}
              compact
              onSuccess={() => setActionError(null)}
              onError={setActionError}
            />
          ))}
        </div>
        {actionError && (
          <p className="mt-1 text-right text-[11px] text-danger">{actionError}</p>
        )}
      </td>
    </tr>
  );
}

/** Flat rows over the same cards (#767): title, due, priority, list, tag chips — each
 *  column client-side sortable (stable; missing values last) — plus the same per-card
 *  actions the kanban cards carry. Columns a payload never uses are omitted entirely. */
function BoardListView({
  module,
  pageId,
  cards,
}: {
  module: string;
  pageId: string;
  cards: BoardCard[];
}) {
  const [sort, setSort] = useState<ListSort>({ key: "due", dir: 1 });
  const today = ymd(new Date());
  const onSort = (key: ListSortKey) =>
    setSort((prev) => (prev.key === key ? { key, dir: prev.dir === 1 ? -1 : 1 } : { key, dir: 1 }));

  const showPriority = cards.some((c) => c.priority != null);
  const showList = cards.some((c) => c.list_title != null);
  const showTags = cards.some((c) => c.tags.length > 0);
  const sorted = sortCards(cards, sort);

  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <table className="w-full min-w-[36rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-edge text-[11px]">
            <SortHeader label="Task" sortKey="title" sort={sort} onSort={onSort} className="w-full" />
            <SortHeader label="Due" sortKey="due" sort={sort} onSort={onSort} />
            {showPriority && <SortHeader label="Priority" sortKey="priority" sort={sort} onSort={onSort} />}
            {showList && <SortHeader label="List" sortKey="list" sort={sort} onSort={onSort} />}
            {showTags && (
              <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wide text-ink-faint">
                Tags
              </th>
            )}
            <th className="px-3 py-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((card) => (
            <ListRow
              key={card.id}
              module={module}
              pageId={pageId}
              card={card}
              today={today}
              showPriority={showPriority}
              showList={showList}
              showTags={showTags}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ── the Calendar representation (#767) ──────────────────────────────────────── */

/** Chip tone classes — the board's due-badge tone language on a month grid. */
const CHIP_TONE: Record<DueTone, string> = {
  overdue: "border-danger/40 text-danger",
  today: "border-accent/50 bg-accent-dim text-accent-strong",
  upcoming: "border-edge text-ink-dim",
  done: "border-edge text-ink-faint line-through",
};

const MAX_CELL_CHIPS = 3;

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** A month grid placing every card on its due date (#767). A chip opens the full card —
 *  same badges, same actions — in a sheet; undated cards never reach this view (the
 *  module keeps them off the page, #766, and `cardsByDate` drops any strays). */
function BoardCalendarView({
  module,
  pageId,
  cards,
}: {
  module: string;
  pageId: string;
  cards: BoardCard[];
}) {
  const [cursor, setCursor] = useState<MonthCursor>(() => monthOf(new Date()));
  const [openCard, setOpenCard] = useState<BoardCard | null>(null);
  const today = ymd(new Date());
  const byDate = cardsByDate(cards);
  const cells = monthCells(cursor);
  // The sheet shows the live card: after an action refetches the page, re-resolve it by id
  // so e.g. Complete strikes the card through instead of showing stale data.
  const shownCard = openCard ? (cards.find((c) => c.id === openCard.id) ?? null) : null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 px-4 py-2">
        <div className="flex items-center rounded-(--radius-field) border border-edge">
          <button
            aria-label="Previous month"
            onClick={() => setCursor((c) => stepMonth(c, -1))}
            className="rounded-l-(--radius-field) px-1.5 py-1 text-ink-dim transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <ChevronLeft size={16} />
          </button>
          <button
            onClick={() => setCursor(monthOf(new Date()))}
            className="border-x border-edge px-2 py-1 text-xs text-ink-dim transition-colors hover:bg-surface-2 hover:text-ink"
          >
            Today
          </button>
          <button
            aria-label="Next month"
            onClick={() => setCursor((c) => stepMonth(c, 1))}
            className="rounded-r-(--radius-field) px-1.5 py-1 text-ink-dim transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <ChevronRight size={16} />
          </button>
        </div>
        <h2 className="font-serif text-base text-ink">{monthLabel(cursor)}</h2>
      </div>
      <div className="grid shrink-0 grid-cols-7 border-b border-edge">
        {WEEKDAYS.map((w) => (
          <div key={w} className="px-2 py-1.5 text-center text-xs font-medium text-ink-faint">
            {w}
          </div>
        ))}
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-7 grid-rows-6">
        {cells.map((cell) => {
          const dayCards = byDate.get(cell.date) ?? [];
          const overflow = dayCards.length - MAX_CELL_CHIPS;
          return (
            <div
              key={cell.date}
              className={cn(
                "flex min-h-0 flex-col gap-0.5 overflow-hidden border-b border-r border-edge p-1",
                !cell.inMonth && "bg-surface-2/40",
              )}
            >
              <div className="flex justify-end">
                <span
                  className={cn(
                    "flex size-5 items-center justify-center rounded-full text-xs",
                    cell.date === today
                      ? "bg-accent font-medium text-on-accent"
                      : cell.inMonth
                        ? "text-ink-dim"
                        : "text-ink-faint",
                  )}
                >
                  {cell.day}
                </span>
              </div>
              <div className="flex min-h-0 flex-col gap-0.5 overflow-hidden">
                {dayCards.slice(0, MAX_CELL_CHIPS).map((card) => (
                  <button
                    key={card.id}
                    onClick={() => setOpenCard(card)}
                    className={cn(
                      "truncate rounded border px-1 py-0.5 text-left text-[11px] leading-tight transition-colors hover:bg-surface-2",
                      CHIP_TONE[dueTone(card, today)],
                    )}
                  >
                    {card.title}
                  </button>
                ))}
                {overflow > 0 && (
                  <button
                    onClick={() => setOpenCard(dayCards[MAX_CELL_CHIPS])}
                    className="px-1 text-left text-[11px] text-ink-faint hover:text-ink"
                  >
                    +{overflow} more
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {/* A chip opens the ordinary card — same badges, same actions, no new mutation
          path — in a sheet, since a month cell has no room for the actions row. */}
      <Sheet open={shownCard !== null} onClose={() => setOpenCard(null)} title="Task">
        {shownCard && (
          <BoardCardView
            module={module}
            pageId={pageId}
            card={shownCard}
            draggable={false}
            dragging={false}
            onDragStart={() => {}}
            onDragEnd={() => {}}
          />
        )}
      </Sheet>
    </div>
  );
}

export function BoardView({ module, pageId }: { module: string; pageId: string }) {
  const qc = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  // Selected control values, forwarded as query params (ADR-0049). Empty until the operator
  // changes a control — the module's declared defaults drive the first fetch. The reserved
  // `view` param (#767) is seeded up front: an explicit `?view=` deep-link wins, else the
  // per-page stored choice (the #743 localStorage pattern), else the module's default.
  const [params, setParams] = useState<Record<string, string>>(() => {
    const view = searchParams.get(VIEW_CONTROL_ID) ?? readStoredView(module, pageId);
    const initial: Record<string, string> = {};
    if (view) initial[VIEW_CONTROL_ID] = view;
    return initial;
  });
  // Drag-and-drop state (#380): the card under the pointer + its source column, and the column
  // currently hovered (for the drop highlight).
  const [drag, setDrag] = useState<{ card: BoardCard; from: string } | null>(null);
  const [dropCol, setDropCol] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["module-page", module, pageId, params],
    queryFn: () => api.modulePage(module, pageId, params),
    placeholderData: keepPreviousData,
  });

  // Move a task by drag-and-drop via the existing move tool — the page refetches on success
  // so the board reflects the move (#380).
  const move = useMutation({
    mutationFn: ({ action, toListId }: { action: BoardAction; toListId: string }) =>
      api.invokeModuleTool(module, action.tool, { ...action.args, to_list_id: toListId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["module-page", module, pageId] }),
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : "Could not move the task."),
  });

  if (query.isLoading && !query.data) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (query.isError && !query.data) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState quote="This page is resting.">
          <p className="text-sm text-ink-dim">{(query.error as Error).message}</p>
        </EmptyState>
      </div>
    );
  }

  const data = BoardData.parse(query.data ?? {});
  const hasCards = data.columns.some((column) => column.cards.length > 0);

  // The reserved `view` control (#767): only a page that declares it gets the alternate
  // representations; junk from a URL clamps to the kanban board.
  const viewControl = data.controls.find((control) => control.id === VIEW_CONTROL_ID);
  const selectors = data.controls.filter((control) => control.id !== VIEW_CONTROL_ID);
  const requestedView = viewControl ? (params[VIEW_CONTROL_ID] ?? viewControl.value) : BOARD_VIEW;
  const activeView =
    viewControl && (requestedView === LIST_VIEW || requestedView === CALENDAR_VIEW)
      ? requestedView
      : BOARD_VIEW;
  const hasToolbar = data.controls.length > 0 || data.actions.length > 0;

  const setView = (view: string) => {
    setParams((prev) => ({ ...prev, [VIEW_CONTROL_ID]: view }));
    // Persist per page (#743 pattern) and keep the URL deep-linkable: without the URL
    // write, a stale `?view=` would win over this newer choice on the next reload.
    writeStoredView(module, pageId, view);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(VIEW_CONTROL_ID, view);
        return next;
      },
      { replace: true },
    );
  };

  // The dragged card's move action (if any) and whether it can land on a given column.
  const dragMoveAction = drag ? moveActionOf(drag.card) : undefined;
  const canDropOn = (column: BoardColumn): boolean =>
    drag !== null &&
    dragMoveAction !== undefined &&
    column.id !== drag.from &&
    moveTargetFor(dragMoveAction, column.title) !== undefined;

  const handleDrop = (column: BoardColumn) => {
    const dragged = drag;
    setDrag(null);
    setDropCol(null);
    if (!dragged) return;
    const action = moveActionOf(dragged.card);
    if (!action || column.id === dragged.from) return;
    const toListId = moveTargetFor(action, column.title);
    if (!toListId) return;
    move.mutate({ action, toListId });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {hasToolbar && (
        // Follows the shell's toolbar convention (the calendar toolbar, #628/#641): the view
        // controls are grouped into their own cluster (like the calendar's Today/‹/› group) so
        // "Group by" and "Show" wrap and reflow as one cohesive unit rather than splitting
        // independently, and the actions are a second cluster pushed right by `ml-auto` — one
        // coherent line on desktop, a clean stack (never a lone stranded button) on phone (#634).
        // The reserved view switcher (#767) sits rightmost, exactly where the calendar page
        // keeps its own.
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-edge px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-3">
            {selectors.map((control) => (
              <ControlSelect
                key={control.id}
                control={control}
                value={params[control.id] ?? control.value}
                onChange={(value) => setParams((prev) => ({ ...prev, [control.id]: value }))}
              />
            ))}
          </div>
          {(query.isFetching || move.isPending) && <Spinner className="size-3.5 text-ink-faint" />}
          {(data.actions.length > 0 || viewControl) && (
            <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
              {/* iconOnlyNarrow drops the label below `sm`, matching the calendar
                  toolbar's page action (#562) — this row already wraps as a fallback,
                  but shrinking first keeps a single action on one line more often. */}
              {data.actions.map((action) => (
                <ActionControl
                  key={action.tool + action.label}
                  module={module}
                  pageId={pageId}
                  action={action}
                  iconOnlyNarrow
                />
              ))}
              {viewControl && (
                <ViewSwitcher control={viewControl} value={activeView} onChange={setView} />
              )}
            </div>
          )}
        </div>
      )}

      {!hasCards ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <EmptyState quote="Nothing on the board yet." />
        </div>
      ) : activeView === LIST_VIEW ? (
        <BoardListView module={module} pageId={pageId} cards={flattenCards(data.columns)} />
      ) : activeView === CALENDAR_VIEW ? (
        <BoardCalendarView module={module} pageId={pageId} cards={flattenCards(data.columns)} />
      ) : (
        <div className="flex min-h-0 flex-1 gap-4 overflow-x-auto p-4">
          {data.columns.map((column) => (
            <section
              key={column.id}
              onDragOver={(e) => {
                if (!canDropOn(column)) return;
                e.preventDefault(); // allow the drop
                if (dropCol !== column.id) setDropCol(column.id);
              }}
              onDragLeave={(e) => {
                // Only clear when the pointer truly leaves the column, not when it crosses a child.
                if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
                  setDropCol((c) => (c === column.id ? null : c));
                }
              }}
              onDrop={(e) => {
                e.preventDefault();
                handleDrop(column);
              }}
              className="flex w-72 shrink-0 flex-col"
            >
              <header className="mb-2 flex items-center gap-2 px-1">
                <h2 className="text-xs font-medium uppercase tracking-wide text-ink-faint">
                  {column.title}
                </h2>
                <span className="text-xs text-ink-faint">{column.cards.length}</span>
              </header>
              <div
                className={cn(
                  "flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto rounded-(--radius-card) p-1 transition-colors",
                  dropCol === column.id && "bg-accent-dim ring-1 ring-accent/40",
                )}
              >
                {column.cards.map((card) => (
                  <BoardCardView
                    key={card.id}
                    module={module}
                    pageId={pageId}
                    card={card}
                    draggable={moveActionOf(card) !== undefined}
                    dragging={drag?.card.id === card.id}
                    onDragStart={() => setDrag({ card, from: column.id })}
                    onDragEnd={() => {
                      setDrag(null);
                      setDropCol(null);
                    }}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
