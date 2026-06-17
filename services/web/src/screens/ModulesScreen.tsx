/**
 * Modules — every installed module, rendered entirely from its manifest
 * (ADR-0007 Tier 1). Config forms come from the declared JSON Schema, actions
 * invoke the module's MCP tools through the core, and a new module appears
 * here with no UI rebuild. No module code ever runs in this shell.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Play, Plus } from "lucide-react";
import { Fragment, createElement, useState } from "react";

import { SchemaForm, type ObjectSchema } from "@/components/SchemaForm";
import { Badge, Card, Confirm, Dot, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { moduleIcon } from "@/lib/icons";
import type { ModuleSnapshot, ToolSpec, UiAction } from "@/lib/contracts";

function ActionRow({ module, action }: { module: string; action: UiAction; }) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState<Record<string, unknown> | null>(null);
  const invoke = useMutation({
    mutationFn: (args: Record<string, unknown>) => api.invokeModuleTool(module, action.tool, args),
  });
  const modules = useQuery({ queryKey: ["modules"], queryFn: api.modules });
  const tool: ToolSpec | undefined = modules.data
    ?.find((m) => m.manifest.name === module)
    ?.manifest.tools.find((t) => t.name === action.tool);

  const run = (args: Record<string, unknown>) => {
    if (action.confirm) setConfirming(args);
    else invoke.mutate(args);
  };

  return (
    <div className="rounded-(--radius-field) border border-edge p-3">
      <button className="flex w-full items-center gap-2 text-left" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="flex-1">
          <span
            className={cn(
              "text-sm",
              action.intent === "danger" ? "text-danger" : "text-ink",
            )}
          >
            {action.label}
          </span>
          {action.description && (
            <span className="block text-xs text-ink-dim">{action.description}</span>
          )}
        </span>
        <Play size={14} className="text-ink-faint" />
      </button>
      {open && (
        <div className="mt-3 border-t border-edge pt-3">
          <SchemaForm
            schema={(tool?.input_schema ?? {}) as ObjectSchema}
            submitLabel={action.label}
            busy={invoke.isPending}
            onSubmit={run}
          />
          {invoke.isSuccess && (
            <pre className="mt-3 max-h-40 overflow-auto rounded-(--radius-field) bg-surface-2 p-2.5 font-mono text-[12px] text-ink-dim">
              {invoke.data.result || "(empty result)"}
            </pre>
          )}
          {invoke.isError && (
            <p className="mt-2 text-sm text-danger">{(invoke.error as Error).message}</p>
          )}
        </div>
      )}
      <Confirm
        open={confirming !== null}
        danger={action.intent === "danger"}
        message={action.confirm ?? ""}
        confirmLabel={action.label}
        onCancel={() => setConfirming(null)}
        onConfirm={() => {
          if (confirming) invoke.mutate(confirming);
          setConfirming(null);
        }}
      />
    </div>
  );
}

function ModuleStatus({ name }: { name: string }) {
  const status = useQuery({
    queryKey: ["module-status", name],
    queryFn: () => api.moduleStatus(name),
    refetchInterval: 60_000,
  });

  if (status.isLoading) return <Spinner />;
  if (!status.data || Object.keys(status.data).length === 0) return null;

  return (
    <div>
      <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">Status</h4>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {Object.entries(status.data).map(([k, v]) => (
          <Fragment key={k}>
            <dt className="text-ink-dim">{k.replace(/_/g, " ")}</dt>
            <dd className="text-ink">{v == null ? "—" : String(v)}</dd>
          </Fragment>
        ))}
      </dl>
    </div>
  );
}

function ModuleConfig({ snapshot }: { snapshot: ModuleSnapshot }) {
  const name = snapshot.manifest.name;
  const queryClient = useQueryClient();
  const config = useQuery({
    queryKey: ["module-config", name],
    queryFn: () => api.moduleConfig(name),
  });
  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.saveModuleConfig(name, values),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["module-config", name] }),
  });
  const schema = snapshot.manifest.ui?.config_schema as ObjectSchema | undefined;
  if (!schema || Object.keys(schema.properties ?? {}).length === 0) return null;

  return (
    <div>
      <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">Settings</h4>
      {config.isLoading ? (
        <Spinner />
      ) : (
        <SchemaForm
          schema={schema}
          initial={config.data ?? {}}
          submitLabel={save.isSuccess ? "Saved" : "Save settings"}
          busy={save.isPending}
          onSubmit={(values) => save.mutate(values)}
        />
      )}
      {save.isError && <p className="mt-2 text-sm text-danger">{(save.error as Error).message}</p>}
    </div>
  );
}

function ModuleCard({ snapshot }: { snapshot: ModuleSnapshot }) {
  const [open, setOpen] = useState(false);
  const { manifest, status } = snapshot;
  const ui = manifest.ui;
  const known = ui == null || ui.ui_version === "1";

  return (
    <Card className="p-0">
      <button
        className="flex w-full items-center gap-3 p-4 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="flex size-10 items-center justify-center rounded-(--radius-field) border border-edge bg-surface-2 text-accent">
          {createElement(moduleIcon(ui?.icon ?? "puzzle"), { size: 19 })}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="font-serif text-base text-ink">{manifest.name}</span>
            <Badge tone="dim">v{status.version ?? manifest.version}</Badge>
          </span>
          <span className="mt-0.5 block truncate text-sm text-ink-dim">
            {ui?.summary || manifest.description || "—"}
          </span>
        </span>
        <span className="flex items-center gap-2">
          <Dot tone={status.healthy ? "ok" : "danger"} />
          {open ? <ChevronDown size={16} className="text-ink-faint" /> : <ChevronRight size={16} className="text-ink-faint" />}
        </span>
      </button>

      {open && (
        <div className="flex flex-col gap-5 border-t border-edge p-4">
          {!status.healthy && (
            <p className="text-sm text-warn">
              Unreachable right now — the card shows the last known manifest.
            </p>
          )}
          {!known && (
            <p className="text-sm text-warn">
              This module speaks a newer UI vocabulary (v{ui?.ui_version}) than this shell.
            </p>
          )}

          {known && status.healthy && ui?.status_url && <ModuleStatus name={manifest.name} />}

          {known && status.healthy && <ModuleConfig snapshot={snapshot} />}

          {known && status.healthy && (ui?.actions.length ?? 0) > 0 && (
            <div>
              <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
                Actions
              </h4>
              <div className="flex flex-col gap-2">
                {ui!.actions.map((action) => (
                  <ActionRow key={action.tool + action.label} module={manifest.name} action={action} />
                ))}
              </div>
            </div>
          )}

          {manifest.tools.length > 0 && (
            <div>
              <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
                Tools the agent can use
              </h4>
              <div className="flex flex-wrap gap-1.5">
                {manifest.tools.map((tool) => (
                  <Badge key={tool.name} tone="dim" className="font-mono">
                    {tool.name}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {(manifest.events_emitted.length > 0 || manifest.events_consumed.length > 0) && (
            <div className="text-xs text-ink-faint">
              {manifest.events_emitted.length > 0 && (
                <p>emits: {manifest.events_emitted.map((e) => e.subject).join(", ")}</p>
              )}
              {manifest.events_consumed.length > 0 && (
                <p>listens: {manifest.events_consumed.map((e) => e.subject).join(", ")}</p>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

export function ModulesScreen() {
  const modules = useQuery({
    queryKey: ["modules"],
    queryFn: api.modules,
    refetchInterval: 30_000,
  });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <div>
          <h1 className="font-serif text-xl text-ink">Modules</h1>
          <p className="mt-1 text-sm text-ink-dim">
            Each capability is its own container. Add one to the stack and its
            settings, status and actions appear here — drawn from its manifest, no
            rebuild, no module code in this app.
          </p>
        </div>
        {modules.isLoading && <Spinner />}
        {modules.isError && (
          <Card className="border-warn/40 text-sm text-warn">
            The core is unreachable — module discovery is down.
          </Card>
        )}
        {modules.data?.map((snapshot) => (
          <ModuleCard key={snapshot.manifest.name} snapshot={snapshot} />
        ))}
        <button
          className="flex items-center justify-center gap-2 rounded-(--radius-card) border border-dashed border-edge-strong px-4 py-6 text-sm text-ink-faint"
          disabled
          title="One-click install by domain arrives with the module installer"
        >
          <Plus size={16} />
          add a module — one-click install arrives in a later phase
        </button>
      </div>
    </div>
  );
}
