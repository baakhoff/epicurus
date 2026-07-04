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
 * Columns scroll horizontally (kanban-style) on every width.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge, EmptyState, Select, Spinner, cn } from "@/components/ui";
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
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function BoardView({ module, pageId }: { module: string; pageId: string }) {
  const qc = useQueryClient();
  // Selected control values, forwarded as query params (ADR-0049). Empty until the operator
  // changes a control — the module's declared defaults drive the first fetch.
  const [params, setParams] = useState<Record<string, string>>({});
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
  const hasToolbar = data.controls.length > 0 || data.actions.length > 0;

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
        // A wrapping toolbar: view controls on the left, board actions on the right. It wraps
        // (gap-y) so on a narrow phone the controls and the Add button get their own lines and
        // breathing room rather than a lone button glued to the top-right corner.
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-edge px-3 py-2.5">
          {data.controls.map((control) => (
            <ControlSelect
              key={control.id}
              control={control}
              value={params[control.id] ?? control.value}
              onChange={(value) => setParams((prev) => ({ ...prev, [control.id]: value }))}
            />
          ))}
          {(query.isFetching || move.isPending) && <Spinner className="size-3.5 text-ink-faint" />}
          {data.actions.length > 0 && (
            <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
              {data.actions.map((action) => (
                <ActionControl
                  key={action.tool + action.label}
                  module={module}
                  pageId={pageId}
                  action={action}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {!hasCards ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <EmptyState quote="Nothing on the board yet." />
        </div>
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
