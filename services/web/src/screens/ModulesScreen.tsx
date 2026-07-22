/**
 * Modules — every installed module, rendered entirely from its manifest
 * (ADR-0007 Tier 1). Config forms come from the declared JSON Schema, actions
 * invoke the module's MCP tools through the core, and a new module appears
 * here with no UI rebuild. No module code ever runs in this shell.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  ChevronUp,
  GripVertical,
  Play,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { Fragment, createElement, useState } from "react";

import { modulePageNavs, sortByPageOrder, type ModulePageNav } from "@/app/registry";
import { SchemaForm, type ObjectSchema } from "@/components/SchemaForm";
import { Badge, Button, Card, Confirm, Dot, Select, Spinner, Switch, TextInput, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { moduleIcon } from "@/lib/icons";
import { CORE_MODULE } from "@/lib/suggestions";
import type {
  Collection,
  CollectionPrefs,
  CollectionRef,
  ModuleSnapshot,
  ToolSpec,
  UiAction,
} from "@/lib/contracts";

/**
 * Reorder card (#543): drag-and-drop (mirroring the tasks board's native HTML5 pattern, #380)
 * plus Up/Down buttons for keyboard/WCAG-AA access — both paths funnel into the same
 * compute-then-single-mutation flow, matching the board's own "compute locally, one mutation,
 * let the refetch confirm" shape rather than persisting on every drag-over.
 */
function PageOrderCard({ modules }: { modules: ModuleSnapshot[] }) {
  const qc = useQueryClient();
  const orderQuery = useQuery({ queryKey: ["page-order"], queryFn: () => api.pageOrder() });
  const setOrder = useMutation({
    mutationFn: (order: string[]) => api.setPageOrder(order),
    onSuccess: (data) => qc.setQueryData(["page-order"], data),
  });
  const [draggedPath, setDraggedPath] = useState<string | null>(null);
  const [overPath, setOverPath] = useState<string | null>(null);

  const pages: ModulePageNav[] = sortByPageOrder(
    modulePageNavs(modules).filter((p) => p.archetype !== "review"),
    orderQuery.data?.order ?? [],
  );
  if (pages.length < 2) return null;
  const paths = pages.map((p) => p.path);

  const commit = (next: string[]) => {
    if (next.join(" ") === paths.join(" ")) return;
    setOrder.mutate(next);
  };

  const moveBy = (path: string, direction: -1 | 1) => {
    const i = paths.indexOf(path);
    const j = i + direction;
    if (j < 0 || j >= paths.length) return;
    const next = [...paths];
    [next[i], next[j]] = [next[j], next[i]];
    commit(next);
  };

  const dropBefore = (targetPath: string) => {
    const dragged = draggedPath;
    setDraggedPath(null);
    setOverPath(null);
    if (!dragged || dragged === targetPath) return;
    const without = paths.filter((p) => p !== dragged);
    without.splice(without.indexOf(targetPath), 0, dragged);
    commit(without);
  };

  return (
    <Card className="p-0">
      <div className="border-b border-edge px-4 py-3">
        <h2 className="text-sm font-medium text-ink">Page order</h2>
        <p className="mt-0.5 text-xs text-ink-dim">
          Drag to reorder the left nav, or use the arrow buttons. Newly added pages join at the
          end automatically.
        </p>
      </div>
      <ul aria-label="Left-nav page order" className="divide-y divide-edge">
        {pages.map((page, i) => {
          const Icon = moduleIcon(page.icon);
          return (
            <li
              key={page.path}
              draggable
              onDragStart={(e) => {
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/plain", page.path);
                setDraggedPath(page.path);
              }}
              onDragEnd={() => {
                setDraggedPath(null);
                setOverPath(null);
              }}
              onDragOver={(e) => {
                if (!draggedPath || draggedPath === page.path) return;
                e.preventDefault();
                if (overPath !== page.path) setOverPath(page.path);
              }}
              onDragLeave={(e) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
                  setOverPath((p) => (p === page.path ? null : p));
                }
              }}
              onDrop={(e) => {
                e.preventDefault();
                dropBefore(page.path);
              }}
              className={cn(
                "flex items-center gap-2 px-4 py-2.5 transition-colors",
                draggedPath === page.path && "opacity-40",
                overPath === page.path && draggedPath !== page.path && "bg-accent-dim",
              )}
            >
              <GripVertical size={14} className="shrink-0 cursor-grab text-ink-faint active:cursor-grabbing" />
              <Icon size={16} className="shrink-0 text-ink-faint" />
              <span className="flex-1 truncate text-sm text-ink">{page.label}</span>
              <span className="shrink-0 text-xs text-ink-faint">{page.module}</span>
              <div className="flex shrink-0 items-center gap-0.5">
                <button
                  className="rounded p-1 text-ink-faint hover:text-ink disabled:opacity-30"
                  onClick={() => moveBy(page.path, -1)}
                  disabled={i === 0 || setOrder.isPending}
                  aria-label={`Move ${page.label} up`}
                >
                  <ChevronUp size={14} />
                </button>
                <button
                  className="rounded p-1 text-ink-faint hover:text-ink disabled:opacity-30"
                  onClick={() => moveBy(page.path, 1)}
                  disabled={i === pages.length - 1 || setOrder.isPending}
                  aria-label={`Move ${page.label} down`}
                >
                  <ChevronDown size={14} />
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function ActionRow({ module, action }: { module: string; action: UiAction; }) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState<Record<string, unknown> | null>(null);
  const invoke = useMutation({
    mutationFn: (args: Record<string, unknown>) => api.invokeModuleTool(module, action.tool, args),
  });
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules() });
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

function ModuleModels({ snapshot }: { snapshot: ModuleSnapshot }) {
  const name = snapshot.manifest.name;
  const slots = snapshot.manifest.required_models;
  const queryClient = useQueryClient();
  const selections = useQuery({
    queryKey: ["module-models", name],
    queryFn: () => api.getModuleModels(name),
  });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  // Saved hosted ids (#496) are assignable to a chat slot too — a module can run on a hosted
  // model, not only a local one (ADR-0029).
  const saved = useQuery({ queryKey: ["savedModels"], queryFn: () => api.savedModels() });
  const save = useMutation({
    mutationFn: (next: Record<string, string>) => api.setModuleModels(name, next),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["module-models", name] }),
  });
  if (slots.length === 0) return null;

  // Choosing "Core default" (value "") clears the slot; the core falls back to its default.
  const current = selections.data ?? {};
  const available = (models.data ?? []).filter((m) => !m.hidden);
  const hosted = saved.data ?? [];

  return (
    <div>
      <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">Models</h4>
      <div className="flex flex-col gap-3">
        {slots.map((slot) => (
          <label key={slot.key} className="block">
            <span className="text-[13px] text-ink">{slot.label}</span>
            {slot.description && (
              <span className="block text-xs text-ink-dim">{slot.description}</span>
            )}
            <Select
              className="mt-1 w-full"
              value={current[slot.key] ?? ""}
              disabled={save.isPending || selections.isLoading}
              onChange={(e) => save.mutate({ ...current, [slot.key]: e.target.value })}
            >
              <option value="">Core default</option>
              {available.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                </option>
              ))}
              {/* Hosted ids are chat-only — the gateway embeds locally, so an embedding slot
                  never offers them (they'd fail at call time). */}
              {slot.role === "chat" && hosted.length > 0 && (
                <optgroup label="Hosted">
                  {hosted.map((h) => (
                    <option key={h.model} value={h.model}>
                      {h.model}
                    </option>
                  ))}
                </optgroup>
              )}
            </Select>
          </label>
        ))}
      </div>
      {save.isError && <p className="mt-2 text-sm text-danger">{(save.error as Error).message}</p>}
    </div>
  );
}

/** "calendar" → "Calendars", "list" → "Lists" — a section heading from the spec noun. */
function pluralNoun(noun: string): string {
  const cap = noun.charAt(0).toUpperCase() + noun.slice(1);
  return cap.endsWith("s") ? cap : `${cap}s`;
}

const sameRef = (a: CollectionRef, b: { account: string; collection: string }): boolean =>
  a.account === b.account && a.collection === b.collection;

/**
 * Connected accounts + per-collection toggles + an active switcher (ADR-0030) — the
 * core-rendered replacement for the old local/google provider dropdown. The module
 * supplies the data (its `/accounts`, merged with the stored selection); this shell
 * owns the chrome. `local` is the silent default and never appears here.
 */
function ModuleCollections({ snapshot }: { snapshot: ModuleSnapshot }) {
  const name = snapshot.manifest.name;
  const spec = snapshot.manifest.collections;
  const queryClient = useQueryClient();
  const view = useQuery({
    queryKey: ["module-collections", name],
    queryFn: () => api.getModuleCollections(name),
    enabled: spec != null,
  });
  const save = useMutation({
    mutationFn: (prefs: CollectionPrefs) => api.saveModuleCollections(name, prefs),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["module-collections", name] }),
  });
  const connect = useMutation({
    // Request this module's own API scopes for the provider (#241); the core unions them
    // onto the default identity scopes and accumulates any previously-granted ones.
    mutationFn: (provider: string) =>
      api.oauthConnect(provider, (snapshot.manifest.oauth_scopes?.[provider] ?? []).join(" ") || undefined),
    onSuccess: (res) => {
      window.location.href = res.auth_url;
    },
  });
  if (!spec) return null;

  const accounts = view.data?.accounts ?? [];
  const cols = accounts.flatMap((a) => a.collections);
  const enabledRefs: CollectionRef[] = cols
    .filter((c) => c.enabled)
    .map((c) => ({ account: c.account, collection: c.collection }));
  const activeCol = cols.find((c) => c.active) ?? null;

  // Toggling/switching rebuilds the full selection and persists it; `active` must stay
  // within `enabled`, and disabling the active collection falls back to the local default.
  const toggleEnabled = (c: Collection, on: boolean) => {
    const ref = { account: c.account, collection: c.collection };
    const enabled = on
      ? [...enabledRefs.filter((r) => !sameRef(r, ref)), ref]
      : enabledRefs.filter((r) => !sameRef(r, ref));
    const active =
      activeCol && enabled.some((r) => sameRef(r, activeCol))
        ? { account: activeCol.account, collection: activeCol.collection }
        : null;
    save.mutate({ enabled, active });
  };
  const setActive = (c: Collection | null) => {
    if (c === null) {
      save.mutate({ enabled: enabledRefs, active: null });
      return;
    }
    const ref = { account: c.account, collection: c.collection };
    const enabled = enabledRefs.some((r) => sameRef(r, ref)) ? enabledRefs : [...enabledRefs, ref];
    save.mutate({ enabled, active: ref });
  };

  const activeWord = spec.multi ? "default" : "active";

  return (
    <div>
      <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
        {pluralNoun(spec.noun)}
      </h4>
      {view.isLoading ? (
        <Spinner />
      ) : (
        <div className="flex flex-col gap-3">
          {accounts.map((account) => (
            <div key={account.account} className="rounded-(--radius-field) border border-edge p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm text-ink">{account.label}</span>
                {account.connected ? (
                  <Badge tone="ok">connected</Badge>
                ) : (
                  <Button
                    busy={connect.isPending}
                    onClick={() => connect.mutate(account.provider)}
                  >
                    Connect
                  </Button>
                )}
              </div>
              {account.connected && account.collections.length === 0 && (
                <p className="mt-2 text-xs text-ink-dim">No {spec.noun}s found in this account.</p>
              )}
              {account.collections.length > 0 && (
                <div className="mt-2 flex flex-col gap-1.5">
                  {account.collections.map((c) => (
                    <div
                      key={`${c.account}/${c.collection}`}
                      className="flex items-center justify-between gap-3"
                    >
                      <span className="min-w-0 flex-1 truncate text-sm text-ink">{c.title}</span>
                      <div className="flex items-center gap-3">
                        {c.enabled && c.writable && (
                          <button
                            className={cn("text-xs", c.active ? "text-accent" : "text-ink-faint")}
                            aria-pressed={c.active ?? false}
                            disabled={save.isPending}
                            onClick={() => setActive(c.active ? null : c)}
                          >
                            {c.active ? activeWord : `set ${activeWord}`}
                          </button>
                        )}
                        <Switch
                          checked={c.enabled ?? false}
                          onChange={(on) => toggleEnabled(c, on)}
                          disabled={save.isPending}
                          label={`Toggle ${c.title}`}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          <p className="text-xs text-ink-faint">
            {activeCol
              ? `New items are created in “${activeCol.title}”.`
              : `Nothing active — the built-in local default is used.`}
          </p>
        </div>
      )}
      {save.isError && <p className="mt-2 text-sm text-danger">{(save.error as Error).message}</p>}
      {connect.isError && (
        <p className="mt-2 text-sm text-danger">{(connect.error as Error).message}</p>
      )}
    </div>
  );
}

function ToolRow({ module, tool, disabled }: { module: string; tool: string; disabled: boolean }) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setToolEnabled(module, tool, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["modules"] }),
  });
  return (
    <div className="flex items-center justify-between gap-3 rounded-(--radius-field) border border-edge px-3 py-2">
      <span className={cn("font-mono text-xs", disabled ? "text-ink-faint line-through" : "text-ink")}>
        {tool}
      </span>
      <Switch
        checked={!disabled}
        onChange={(next) => toggle.mutate(next)}
        label={`${disabled ? "Enable" : "Disable"} ${tool}`}
      />
    </div>
  );
}

function ModuleCard({
  snapshot,
  onDeferredRemoval,
}: {
  snapshot: ModuleSnapshot;
  onDeferredRemoval: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const { manifest, status, enabled, disabled_tools } = snapshot;
  const ui = manifest.ui;
  const known = ui == null || ui.ui_version === "1";

  // Toggling the registry flag (#126) hides the module's tools/pages/UI; the container
  // keeps running. Refetch the list so the new flag (and the vanished nav page) take hold.
  const toggleEnabled = useMutation({
    mutationFn: (next: boolean) => api.setModuleEnabled(manifest.name, next),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["modules"] }),
  });

  // Confirmed removal (#127, #382) — privileged and destructive, gated by a dialog. Removal
  // is decoupled from the live Docker socket: the module is tombstoned (so it vanishes from
  // this list at once), and when the core has no Docker access its container teardown is
  // deferred to the next restart — we lift that fact to a screen-level notice, since the card
  // itself disappears on success.
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const removeModule = useMutation({
    mutationFn: () => api.removeModule(manifest.name),
    onSuccess: (res) => {
      if (res.container_teardown_deferred) onDeferredRemoval(res.removed);
      queryClient.invalidateQueries({ queryKey: ["modules"] });
    },
  });

  return (
    <Card className="p-0">
      <div className="flex w-full items-center gap-2 p-4">
        <button
          className={cn(
            "flex min-w-0 flex-1 items-center gap-3 text-left",
            !enabled && "opacity-60",
          )}
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <span className="flex size-10 shrink-0 items-center justify-center rounded-(--radius-field) border border-edge bg-surface-2 text-accent">
            {createElement(moduleIcon(ui?.icon ?? "puzzle"), { size: 19 })}
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-2">
              <span className="font-serif text-base text-ink">{manifest.name}</span>
              <Badge tone="dim">v{status.version ?? manifest.version}</Badge>
              {!enabled && <Badge tone="warn">disabled</Badge>}
              {manifest.tags.map((tag) => (
                <Badge key={tag} tone="accent">
                  {tag}
                </Badge>
              ))}
            </span>
            <span className="mt-0.5 block truncate text-sm text-ink-dim">
              {ui?.summary || manifest.description || "—"}
            </span>
          </span>
        </button>
        <Switch
          checked={enabled}
          onChange={(next) => toggleEnabled.mutate(next)}
          label={`${enabled ? "Disable" : "Enable"} ${manifest.name}`}
          disabled={toggleEnabled.isPending}
        />
        <Dot tone={status.healthy ? "ok" : "danger"} />
        <button
          className="text-ink-faint"
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? "Collapse" : "Expand"}
        >
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>
      </div>

      {open && (
        <div className="flex flex-col gap-5 border-t border-edge p-4">
          {!enabled && (
            <p className="text-sm text-ink-dim">
              Disabled — hidden from the agent and the left-nav. The container keeps running;
              re-enable any time with the toggle above.
            </p>
          )}
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

          {known && status.healthy && manifest.collections && (
            <ModuleCollections snapshot={snapshot} />
          )}

          {known && <ModuleModels snapshot={snapshot} />}

          {known && status.healthy && enabled && (ui?.actions.length ?? 0) > 0 && (
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
              <div className="flex flex-col gap-1.5">
                {manifest.tools.map((tool) => (
                  <ToolRow
                    key={tool.name}
                    module={manifest.name}
                    tool={tool.name}
                    disabled={disabled_tools.includes(tool.name)}
                  />
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

          <div className="border-t border-edge pt-4">
            <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
              Danger zone
            </h4>
            <Button
              variant="danger"
              busy={removeModule.isPending}
              onClick={() => setConfirmingRemove(true)}
            >
              <Trash2 size={14} /> Remove module
            </Button>
            <p className="mt-1.5 text-xs text-ink-faint">
              Removes this module and stops + deletes its container. Without Docker access the
              core still removes the module immediately; its container keeps running until the
              next restart. Core, the web shell, and the data plane can never be removed; the
              module stays gone until you redeploy it.
            </p>
            {removeModule.isError && (
              <p className="mt-2 text-sm text-danger">{(removeModule.error as Error).message}</p>
            )}
          </div>
        </div>
      )}
      <Confirm
        open={confirmingRemove}
        danger
        message={`Remove the “${manifest.name}” module? This stops and deletes its container, and it stays removed until you redeploy it. If the core has no Docker access the module is removed anyway, but its container keeps running until the next restart.`}
        confirmLabel="Remove module"
        onCancel={() => setConfirmingRemove(false)}
        onConfirm={() => {
          removeModule.mutate();
          setConfirmingRemove(false);
        }}
      />
    </Card>
  );
}

/** Match a module against a free-text query over its name, description, and tags (#126). */
function matchesQuery(snapshot: ModuleSnapshot, query: string): boolean {
  const m = snapshot.manifest;
  const haystack = [m.name, m.description, m.ui?.summary ?? "", ...m.tags].join(" ").toLowerCase();
  return haystack.includes(query);
}

export function ModulesScreen() {
  const queryClient = useQueryClient();
  const modules = useQuery({
    queryKey: ["modules"],
    queryFn: () => api.modules(),
    refetchInterval: 30_000,
  });
  // Bypasses the core's short-TTL probe cache for an immediate fleet-wide re-probe (#478) —
  // the 30s poll picks up a health change on its own eventually, but this gives the operator
  // an explicit "check now" right after e.g. restarting a container.
  const refresh = useMutation({
    mutationFn: () => api.modules({ refresh: true }),
    onSuccess: (data) => queryClient.setQueryData(["modules"], data),
  });
  // Proactive Docker-reachability status (#622) — so the operator sees what's deferred (never
  // "removal disabled": that always works, ADR-0056/#382) without attempting a removal first.
  const dockerStatus = useQuery({ queryKey: ["docker-status"], queryFn: api.dockerStatus });
  const [query, setQuery] = useState("");
  // Modules removed while the core had no Docker socket (#382): the module is gone from the
  // list, but its container is still running until the next restart — surfaced as an
  // informational (not error) notice the operator can dismiss.
  const [deferred, setDeferred] = useState<string[]>([]);

  const q = query.trim().toLowerCase();
  // Drop the reserved pseudo-module (ADR-0093 §2). It rides the modules list so the shell can
  // discover its `review` page like any module's, but this screen manages what the operator
  // *installed*: the core has no container to enable, configure, or remove (the server refuses
  // all three with a 403), so a card for it would be a row of broken controls.
  const all = (modules.data ?? []).filter((s) => s.manifest.name !== CORE_MODULE);
  const filtered = q ? all.filter((s) => matchesQuery(s, q)) : all;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <div>
          <h1 className="font-serif text-xl text-ink">Modules</h1>
          <p className="mt-1 text-sm text-ink-dim">
            Each capability is its own container. Add one to the stack and its settings,
            status and actions appear here — drawn from its manifest, no rebuild, no module
            code in this app. Toggle a module off to hide it from the agent and the left-nav;
            the container keeps running.
          </p>
        </div>
        {dockerStatus.data && !dockerStatus.data.available && (
          <Card className="text-sm text-ink-dim">
            <p>
              <span className="text-ink">Docker isn't reachable from the core right now</span>
              {dockerStatus.data.reason ? ` (${dockerStatus.data.reason})` : ""}. Module removal
              still works immediately — only deleting its container, and applying an Ollama
              KV-cache change, defers until the next restart.
            </p>
            <p className="mt-1.5 text-xs text-ink-faint">
              To enable immediate cleanup: set <code className="font-mono">DOCKER_GID</code> to
              your host's docker-socket group id and bring the stack up with the opt-in{" "}
              <code className="font-mono">compose.docker-socket.yaml</code> override (see{" "}
              <code className="font-mono">docs/infrastructure/index.md</code>, "Docker-socket
              access") — the socket is root-equivalent, so this is a deliberate grant, not a
              default.
            </p>
          </Card>
        )}
        {all.length > 0 && (
          <div className="flex items-center gap-2">
            <TextInput
              className="flex-1"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search modules by name, description, or tag…"
              aria-label="Search modules"
            />
            <button
              className="flex shrink-0 items-center justify-center rounded-(--radius-field) border border-edge p-2.5 text-ink-faint hover:text-ink disabled:opacity-50"
              onClick={() => refresh.mutate()}
              disabled={refresh.isPending}
              aria-label="Refresh module health"
              title="Refresh module health"
            >
              <RefreshCw size={14} className={refresh.isPending ? "animate-spin" : undefined} />
            </button>
          </div>
        )}
        <PageOrderCard modules={all} />
        {deferred.length > 0 && (
          <Card className="border-accent/40 bg-accent-dim text-sm text-ink-dim">
            {deferred.length === 1 ? (
              <p>
                <span className="text-ink">“{deferred[0]}” removed.</span> Its container is
                still running because the core has no Docker access; it will be cleared on the
                next restart.
              </p>
            ) : (
              <p>
                <span className="text-ink">{deferred.length} modules removed.</span> Their
                containers are still running because the core has no Docker access; they will be
                cleared on the next restart.
              </p>
            )}
            <button
              className="mt-2 text-xs text-accent-strong"
              onClick={() => setDeferred([])}
            >
              Dismiss
            </button>
          </Card>
        )}
        {modules.isLoading && <Spinner />}
        {modules.isError && (
          <Card className="border-warn/40 text-sm text-warn">
            The core is unreachable — module discovery is down.
          </Card>
        )}
        {filtered.map((snapshot) => (
          <ModuleCard
            key={snapshot.manifest.name}
            snapshot={snapshot}
            onDeferredRemoval={(name) =>
              setDeferred((prev) => (prev.includes(name) ? prev : [...prev, name]))
            }
          />
        ))}
        {!modules.isLoading && q !== "" && filtered.length === 0 && (
          <p className="text-sm text-ink-dim">No modules match “{query}”.</p>
        )}
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
