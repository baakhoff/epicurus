/**
 * ActionControl — renders one declarative page action (ADR-0024) as a button with its
 * form / confirm overlay, and invokes the named MCP tool through the core's tool proxy
 * (`invokeModuleTool`, validated against the manifest). On success it refetches the
 * page so the view reflects the change.
 *
 * Shared by every mutating archetype: the `board` cards/columns and the editable
 * `calendar` (#208). The action vocabulary is `BoardAction` (tool, label, icon, intent,
 * args, form/fields/form_values/field_options, confirm).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { createElement, useMemo, useState } from "react";

import { SchemaForm, type ObjectSchema } from "@/components/SchemaForm";
import { Button, Confirm, Sheet, Tooltip, cn } from "@/components/ui";
import { api } from "@/lib/api";
import type { BoardAction } from "@/lib/contracts";
import { moduleIcon } from "@/lib/icons";

/** The argument schema a module tool declares, read from the cached manifest. */
function useToolSchema(module: string, tool: string): ObjectSchema | undefined {
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules() });
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
  fieldOptions?: Record<string, string[]>,
  fieldChoices?: Record<string, { value: string; label: string }[]>,
): ObjectSchema {
  const properties: NonNullable<ObjectSchema["properties"]> = schema?.properties ?? {};
  const argKeys = new Set(Object.keys(args));
  const keys =
    fields && fields.length
      ? fields.filter((key) => key in properties)
      : Object.keys(properties).filter((key) => !argKeys.has(key));
  const picked: NonNullable<ObjectSchema["properties"]> = {};
  for (const key of keys) {
    const base = properties[key] ?? {};
    const choices = fieldChoices?.[key];
    const opts = fieldOptions?.[key];
    // Overlay field_choices / field_options as an enum so SchemaForm renders a <select>.
    // Flatten to a plain string enum (dropping any `anyOf` from an optional param) so the
    // enum survives `resolveProp`; carry labels for field_choices.
    if (choices) {
      picked[key] = {
        type: "string",
        enum: choices.map((c) => c.value),
        enumLabels: choices.map((c) => c.label),
        title: base.title,
        description: base.description,
      };
    } else if (opts) {
      picked[key] = { type: "string", enum: opts, title: base.title, description: base.description };
    } else {
      picked[key] = base;
    }
  }
  const required = (schema?.required ?? []).filter((key) => keys.includes(key));
  return { type: "object", properties: picked, required };
}

/** A single page action rendered as a button, with its form / confirm overlay. */
export function ActionControl({
  module,
  pageId,
  action,
  compact = false,
  size,
  onSuccess,
}: {
  module: string;
  pageId: string;
  action: BoardAction;
  compact?: boolean;
  /** Passed through to the full (non-compact) Button — shrink a denser toolbar (#427). */
  size?: "sm" | "md";
  /** Called after a successful invocation (e.g. to close an event-detail modal). */
  onSuccess?: () => void;
}) {
  const queryClient = useQueryClient();
  const [sheetOpen, setSheetOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const invoke = useMutation({
    mutationFn: (args: Record<string, unknown>) => api.invokeModuleTool(module, action.tool, args),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["module-page", module, pageId] });
      setSheetOpen(false);
      onSuccess?.();
    },
  });
  const schema = useToolSchema(module, action.tool);
  const formSchema = useMemo(
    () =>
      pickSchema(
        schema,
        action.fields ?? undefined,
        action.args,
        action.field_options,
        action.field_choices,
      ),
    [schema, action.fields, action.args, action.field_options, action.field_choices],
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
  // Icon-only (#337): a compact square button whose label lives in a tooltip + aria-label.
  // Only when the module both asks for it and supplies an icon to show.
  const iconOnly = action.icon_only && Boolean(action.icon);

  return (
    <>
      {iconOnly ? (
        <Tooltip label={action.label} side="bottom">
          <button
            type="button"
            onClick={onClick}
            disabled={invoke.isPending}
            aria-label={action.label}
            className={cn(
              "inline-flex items-center justify-center rounded-(--radius-field) p-2 transition-colors disabled:opacity-50",
              action.intent === "danger"
                ? "border border-danger/40 text-danger hover:bg-danger/10"
                : action.intent === "primary"
                  ? "bg-accent text-canvas hover:bg-accent-strong"
                  : "border border-edge-strong text-ink hover:border-accent hover:text-accent-strong",
            )}
          >
            {invoke.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              createElement(moduleIcon(action.icon ?? undefined), { size: 16 })
            )}
          </button>
        </Tooltip>
      ) : compact ? (
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
        <Button variant={fullVariant} size={size} busy={invoke.isPending} onClick={onClick}>
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
