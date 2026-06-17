/**
 * The `board` archetype (ADR-0018): columns of cards, core-rendered. The module
 * supplies only data — columns, cards, and declarative *actions* — through the core
 * page proxy; this screen renders it in ε style. No module markup runs here.
 *
 * Unlike `browser`, a board mutates: each action names one of the module's MCP
 * tools, which the shell invokes through the core (`invokeModuleTool`, validated
 * against the manifest). A `form` action collects arguments via the shared
 * SchemaForm first; a `confirm` action gates a one-tap call behind a dialog. After
 * any successful call the page data is refetched, so the board reflects the change.
 *
 * Columns scroll horizontally (kanban-style) on every width.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { createElement, useMemo, useState } from "react";

import { SchemaForm, type ObjectSchema } from "@/components/SchemaForm";
import { Badge, Button, Confirm, EmptyState, Sheet, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { BoardData, type BoardAction, type BoardCard } from "@/lib/contracts";
import { moduleIcon } from "@/lib/icons";

/** The argument schema a module tool declares, read from the cached manifest. */
function useToolSchema(module: string, tool: string): ObjectSchema | undefined {
  const modules = useQuery({ queryKey: ["modules"], queryFn: api.modules });
  const spec = modules.data
    ?.find((m) => m.manifest.name === module)
    ?.manifest.tools.find((t) => t.name === tool);
  return spec?.input_schema as ObjectSchema | undefined;
}

/**
 * Narrow a tool's input schema to the fields a form action should show: the
 * `fields` whitelist when given, otherwise every property not already fixed by
 * `args`. Required keys outside the shown set are dropped (they come from `args`).
 */
function pickSchema(
  schema: ObjectSchema | undefined,
  fields: string[] | undefined,
  args: Record<string, unknown>,
): ObjectSchema {
  const properties: NonNullable<ObjectSchema["properties"]> = schema?.properties ?? {};
  const argKeys = new Set(Object.keys(args));
  const keys =
    fields && fields.length
      ? fields.filter((key) => key in properties)
      : Object.keys(properties).filter((key) => !argKeys.has(key));
  const picked: NonNullable<ObjectSchema["properties"]> = {};
  for (const key of keys) picked[key] = properties[key];
  const required = (schema?.required ?? []).filter((key) => keys.includes(key));
  return { type: "object", properties: picked, required };
}

/** A single board action rendered as a button, with its form / confirm overlay. */
function BoardActionControl({
  module,
  pageId,
  action,
  compact = false,
}: {
  module: string;
  pageId: string;
  action: BoardAction;
  compact?: boolean;
}) {
  const queryClient = useQueryClient();
  const [sheetOpen, setSheetOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const invoke = useMutation({
    mutationFn: (args: Record<string, unknown>) => api.invokeModuleTool(module, action.tool, args),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["module-page", module, pageId] });
      setSheetOpen(false);
    },
  });
  const schema = useToolSchema(module, action.tool);
  const formSchema = useMemo(
    () => pickSchema(schema, action.fields ?? undefined, action.args),
    [schema, action.fields, action.args],
  );

  const onClick = () => {
    if (action.form) setSheetOpen(true);
    else if (action.confirm) setConfirmOpen(true);
    else invoke.mutate({ ...action.args });
  };

  const compactTone =
    action.intent === "danger"
      ? "text-danger hover:bg-danger/10"
      : action.intent === "primary"
        ? "text-accent-strong hover:bg-accent-dim"
        : "text-ink-dim hover:bg-surface-2 hover:text-ink";
  const fullVariant =
    action.intent === "danger" ? "danger" : action.intent === "primary" ? "primary" : "outline";

  return (
    <>
      {compact ? (
        <button
          type="button"
          onClick={onClick}
          disabled={invoke.isPending}
          className={cn(
            "inline-flex items-center gap-1 rounded-(--radius-field) px-2 py-1 text-xs transition-colors disabled:opacity-50",
            compactTone,
          )}
        >
          {invoke.isPending ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            action.icon && createElement(moduleIcon(action.icon), { size: 13 })
          )}
          {action.label}
        </button>
      ) : (
        <Button variant={fullVariant} busy={invoke.isPending} onClick={onClick}>
          {action.icon && createElement(moduleIcon(action.icon), { size: 15 })}
          {action.label}
        </Button>
      )}

      {!action.form && invoke.isError && (
        <span className="text-[11px] text-danger">{(invoke.error as Error).message}</span>
      )}

      {action.form && (
        <Sheet open={sheetOpen} onClose={() => setSheetOpen(false)} title={action.label}>
          <SchemaForm
            schema={formSchema}
            initial={action.form_values}
            submitLabel={action.label}
            busy={invoke.isPending}
            onSubmit={(values) => invoke.mutate({ ...action.args, ...values })}
          />
          {invoke.isError && (
            <p className="mt-3 text-sm text-danger">{(invoke.error as Error).message}</p>
          )}
        </Sheet>
      )}

      {action.confirm && (
        <Confirm
          open={confirmOpen}
          danger={action.intent === "danger"}
          message={action.confirm}
          confirmLabel={action.label}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => {
            invoke.mutate({ ...action.args });
            setConfirmOpen(false);
          }}
        />
      )}
    </>
  );
}

function BoardCardView({
  module,
  pageId,
  card,
}: {
  module: string;
  pageId: string;
  card: BoardCard;
}) {
  return (
    <div className="rounded-(--radius-card) border border-edge bg-surface p-3">
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
            <BoardActionControl
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
  const query = useQuery({
    queryKey: ["module-page", module, pageId],
    queryFn: () => api.modulePage(module, pageId),
  });

  if (query.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (query.isError) {
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

  return (
    <div className="flex h-full min-h-0 flex-col">
      {data.actions.length > 0 && (
        <div className="flex items-center justify-end gap-2 px-4 py-2">
          {data.actions.map((action) => (
            <BoardActionControl
              key={action.tool + action.label}
              module={module}
              pageId={pageId}
              action={action}
            />
          ))}
        </div>
      )}

      {!hasCards ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <EmptyState quote="Nothing on the board yet." />
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 gap-4 overflow-x-auto p-4">
          {data.columns.map((column) => (
            <section key={column.id} className="flex w-72 shrink-0 flex-col">
              <header className="mb-2 flex items-center gap-2 px-1">
                <h2 className="text-xs font-medium uppercase tracking-wide text-ink-faint">
                  {column.title}
                </h2>
                <span className="text-xs text-ink-faint">{column.cards.length}</span>
              </header>
              <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto pr-1">
                {column.cards.map((card) => (
                  <BoardCardView key={card.id} module={module} pageId={pageId} card={card} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
