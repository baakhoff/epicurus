/**
 * Models — the model manager. Local models (pull with live progress, delete,
 * loaded state, hide from pickers, set as global default) and hosted providers
 * (key entry → core → OpenBao; the key never comes back).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, KeyRound, Search, Star, Trash2 } from "lucide-react";
import { useState } from "react";

import {
  Badge,
  Button,
  Card,
  Confirm,
  Dot,
  Label,
  Sheet,
  Spinner,
  TextInput,
  cn,
} from "@/components/ui";
import { ALL_TAGS, CATALOG, TAG_LABELS, filterCatalog, formatGb, type CatalogTag } from "@/data/catalog";
import { api } from "@/lib/api";
import { PROVIDER_LABELS, PROVIDER_MODEL_HINTS, formatBytes } from "@/lib/format";
import type { ProviderInfo } from "@/lib/contracts";
import { useDownloads } from "@/stores/downloads";
import { usePrefs } from "@/stores/prefs";

// ── Download tray ─────────────────────────────────────────────────────────────

function DownloadTray() {
  const active = useDownloads((s) => s.active);
  const dismiss = useDownloads((s) => s.dismiss);
  const entries = Object.values(active);
  if (entries.length === 0) return null;

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Downloads</h3>
      <div className="flex flex-col gap-2">
        {entries.map((download) => {
          const pct =
            download.total && download.completed != null
              ? Math.min(100, Math.round((download.completed / download.total) * 100))
              : null;
          return (
            <div key={download.model} className="rounded-(--radius-field) border border-edge p-3">
              <div className="flex items-center justify-between text-sm">
                <span className="font-medium text-ink">{download.model}</span>
                <span className={cn("text-xs", download.error ? "text-danger" : "text-ink-dim")}>
                  {download.error ?? download.status}
                  {pct != null && !download.done && ` · ${pct}%`}
                </span>
              </div>
              {!download.done && (
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-2">
                  <div
                    className={cn(
                      "h-full rounded-full bg-accent transition-all",
                      pct == null && "ep-breathe w-1/3",
                    )}
                    style={pct != null ? { width: `${pct}%` } : undefined}
                  />
                </div>
              )}
              {download.total != null && download.completed != null && !download.done && (
                <p className="mt-1 text-[11px] text-ink-faint">
                  {formatBytes(download.completed)} of {formatBytes(download.total)}
                </p>
              )}
              {download.done && (
                <div className="mt-2">
                  <Button variant="ghost" onClick={() => dismiss(download.model)}>
                    Dismiss
                  </Button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ── Catalog browser ───────────────────────────────────────────────────────────

export function CatalogBrowser({ installed }: { installed: Set<string> }) {
  const queryClient = useQueryClient();
  const pull = useDownloads((s) => s.pull);
  const active = useDownloads((s) => s.active);
  const [query, setQuery] = useState("");
  const [activeTag, setActiveTag] = useState<CatalogTag | null>(null);

  const entries = filterCatalog(CATALOG, query, activeTag);

  const startPull = (id: string) => {
    void pull(id, () => queryClient.invalidateQueries({ queryKey: ["models"] }));
  };

  return (
    <Card>
      <h3 className="mb-3 font-serif text-base text-ink">Browse models</h3>

      {/* Search */}
      <div className="relative mb-3">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
        <TextInput
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by name, family, or description…"
          aria-label="Search catalog"
          className="pl-8"
        />
      </div>

      {/* Tag filters */}
      <div className="mb-4 flex flex-wrap gap-1.5">
        <button
          onClick={() => setActiveTag(null)}
          className={cn(
            "rounded-full border px-3 py-1 text-xs transition-colors",
            activeTag === null
              ? "border-accent bg-accent-dim text-accent-strong"
              : "border-edge text-ink-dim hover:border-edge-strong hover:text-ink",
          )}
        >
          All
        </button>
        {ALL_TAGS.map((tag) => (
          <button
            key={tag}
            onClick={() => setActiveTag(activeTag === tag ? null : tag)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs transition-colors",
              activeTag === tag
                ? "border-accent bg-accent-dim text-accent-strong"
                : "border-edge text-ink-dim hover:border-edge-strong hover:text-ink",
            )}
          >
            {TAG_LABELS[tag]}
          </button>
        ))}
      </div>

      {/* Entries */}
      {entries.length === 0 ? (
        <p className="py-4 text-center text-sm text-ink-dim">No models match your search.</p>
      ) : (
        <div className="flex flex-col divide-y divide-edge">
          {entries.map((entry) => {
            const dl = active[entry.id];
            const inProgress = dl && !dl.done;
            const isInstalled = installed.has(entry.id);

            return (
              <div key={entry.id} className="flex items-start gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-sm text-ink">{entry.id}</span>
                    <Badge tone="dim">{entry.params}</Badge>
                    <span className="text-xs text-ink-faint">{formatGb(entry.size_gb)}</span>
                  </div>
                  <p className="mt-0.5 text-xs leading-relaxed text-ink-dim">{entry.description}</p>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {entry.tags.map((t) => (
                      <span
                        key={t}
                        className="rounded-full bg-surface-2 px-2 py-0.5 text-[10px] text-ink-faint"
                      >
                        {TAG_LABELS[t]}
                      </span>
                    ))}
                  </div>
                </div>

                <div className="shrink-0 pt-0.5">
                  {isInstalled ? (
                    <Badge tone="ok">Installed</Badge>
                  ) : (
                    <Button
                      variant="outline"
                      busy={!!inProgress}
                      disabled={!!inProgress}
                      onClick={() => startPull(entry.id)}
                    >
                      {inProgress ? "Pulling…" : "Pull"}
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ── Local models ──────────────────────────────────────────────────────────────

function LocalModels() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const prefModel = usePrefs((s) => s.model);
  const setModel = usePrefs((s) => s.setModel);
  const [confirming, setConfirming] = useState<string | null>(null);

  const globalDefault = llmPrefs.data?.global_default ?? null;

  const remove = useMutation({
    mutationFn: api.deleteModel,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  });

  const toggleHidden = useMutation({
    mutationFn: ({ name, hidden }: { name: string; hidden: boolean }) =>
      api.setModelHidden(name, hidden),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["models"] });
      void queryClient.invalidateQueries({ queryKey: ["llmPrefs"] });
    },
  });

  const setDefault = useMutation({
    mutationFn: (model: string | null) => api.setGlobalDefault(model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llmPrefs"] }),
  });

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Local models</h3>
      {models.isLoading && <Spinner />}
      {models.isError && (
        <p className="text-sm text-warn">
          The local runtime is unreachable — is the ollama service up?
        </p>
      )}
      {models.data?.length === 0 && (
        <p className="text-sm text-ink-dim">None yet. Pull one above — it stays on your disk.</p>
      )}
      <div className="flex flex-col gap-1">
        {models.data?.map((model) => (
          <div
            key={model.name}
            className={cn(
              "group flex items-center gap-3 rounded-(--radius-field) px-2 py-2 hover:bg-surface-2",
              model.hidden && "opacity-60",
            )}
          >
            <button
              className="flex min-w-0 flex-1 items-center gap-2 text-left"
              onClick={() => setModel(model.name)}
              title="Use for new chats"
            >
              <span className="truncate text-sm text-ink">{model.name}</span>
              {model.loaded && <Badge tone="ok">loaded</Badge>}
              {globalDefault === model.name && <Badge tone="accent">default</Badge>}
              {prefModel === model.name && <Badge tone="accent">chatting</Badge>}
              {model.hidden && <Badge tone="dim">hidden</Badge>}
            </button>
            <span className="text-xs text-ink-faint">{formatBytes(model.size)}</span>
            <button
              aria-label={globalDefault === model.name ? "Clear global default" : `Set ${model.name} as global default`}
              onClick={() => setDefault.mutate(globalDefault === model.name ? null : model.name)}
              className={cn(
                "rounded p-1.5 transition-opacity",
                globalDefault === model.name
                  ? "text-accent opacity-100"
                  : "text-ink-faint opacity-0 hover:text-accent group-hover:opacity-100",
              )}
            >
              <Star size={15} fill={globalDefault === model.name ? "currentColor" : "none"} />
            </button>
            <button
              aria-label={model.hidden ? `Show ${model.name} in pickers` : `Hide ${model.name} from pickers`}
              onClick={() => toggleHidden.mutate({ name: model.name, hidden: !model.hidden })}
              className="rounded p-1.5 text-ink-faint opacity-0 transition-opacity hover:text-ink group-hover:opacity-100"
            >
              {model.hidden ? <Eye size={15} /> : <EyeOff size={15} />}
            </button>
            <button
              aria-label={`Delete ${model.name}`}
              onClick={() => setConfirming(model.name)}
              className="rounded p-1.5 text-ink-faint opacity-0 transition-opacity hover:text-danger group-hover:opacity-100"
            >
              <Trash2 size={15} />
            </button>
          </div>
        ))}
      </div>
      <Confirm
        open={confirming !== null}
        danger
        message={`Delete ${confirming} from disk? You can pull it again later.`}
        confirmLabel="Delete"
        onCancel={() => setConfirming(null)}
        onConfirm={() => {
          if (confirming) remove.mutate(confirming);
          setConfirming(null);
        }}
      />
    </Card>
  );
}

// ── Embedding default ─────────────────────────────────────────────────────────

function EmbedDefault() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });

  const current = llmPrefs.data?.global_embed_default ?? "";
  const available = (models.data ?? []).filter((m) => !m.hidden);

  const setEmbedDefault = useMutation({
    mutationFn: (model: string | null) => api.setGlobalEmbedDefault(model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llmPrefs"] }),
  });

  return (
    <Card>
      <h3 className="mb-1 font-serif text-base text-ink">Embedding model</h3>
      <p className="mb-3 text-xs leading-relaxed text-ink-dim">
        Global default used when a module has no per-module embedding override. Per-module
        selections in the Modules page take precedence.
      </p>
      {llmPrefs.isLoading ? (
        <Spinner />
      ) : (
        <label className="block">
          <span className="sr-only">Global embedding model</span>
          <select
            className="w-full rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
            value={current}
            disabled={setEmbedDefault.isPending}
            onChange={(e) => setEmbedDefault.mutate(e.target.value || null)}
          >
            <option value="">System default</option>
            {available.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {setEmbedDefault.isError && (
        <p className="mt-2 text-sm text-danger">{(setEmbedDefault.error as Error).message}</p>
      )}
    </Card>
  );
}

// ── Providers ─────────────────────────────────────────────────────────────────

function KeySheet({
  provider,
  onClose,
}: {
  provider: ProviderInfo | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [key, setKey] = useState("");
  const [base, setBase] = useState("");
  const save = useMutation({
    mutationFn: () => api.setProviderKey(provider!.alias, key, base || undefined),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["providers"] });
      onClose();
    },
  });

  if (!provider) return null;
  return (
    <Sheet open onClose={onClose} title={`${PROVIDER_LABELS[provider.alias] ?? provider.alias} key`}>
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (key.trim()) save.mutate();
        }}
      >
        <div>
          <Label hint="Stored in OpenBao by the core. Write-only — it is never shown again.">
            API key
          </Label>
          <TextInput
            type="password"
            autoComplete="off"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="paste the provider key"
          />
        </div>
        {provider.needs_base_url && (
          <div>
            <Label hint="The OpenAI-compatible endpoint this key belongs to.">Base URL</Label>
            <TextInput
              type="url"
              value={base}
              onChange={(e) => setBase(e.target.value)}
              placeholder="https://api.example.com/v1"
            />
          </div>
        )}
        {save.isError && (
          <p className="text-sm text-danger">{(save.error as Error).message}</p>
        )}
        <Button
          type="submit"
          variant="primary"
          busy={save.isPending}
          disabled={!key.trim() || (provider.needs_base_url && !base.trim())}
        >
          Save key
        </Button>
        <p className="text-xs leading-relaxed text-ink-faint">
          Then chat with{" "}
          <code className="font-mono">{PROVIDER_MODEL_HINTS[provider.alias]}</code> (any model id
          the provider serves).
        </p>
      </form>
    </Sheet>
  );
}

function Providers() {
  const queryClient = useQueryClient();
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const [editing, setEditing] = useState<ProviderInfo | null>(null);
  const clear = useMutation({
    mutationFn: api.clearProviderKey,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });

  return (
    <Card>
      <h3 className="mb-1 font-serif text-base text-ink">Hosted providers</h3>
      <p className="mb-3 text-xs leading-relaxed text-ink-dim">
        Keys live in OpenBao, held by the core — modules and this page never see them
        again after you save.
      </p>
      <div className="flex flex-col gap-1">
        {providers.data
          ?.filter((p) => !p.local)
          .map((provider) => (
            <div
              key={provider.alias}
              className={cn(
                "flex items-center gap-3 rounded-(--radius-field) border px-3 py-2.5 transition-colors",
                provider.configured ? "border-accent/40 bg-accent-dim/40" : "border-edge",
              )}
            >
              <Dot tone={provider.configured ? "accent" : "dim"} />
              <div className="min-w-0 flex-1">
                <p className="text-sm text-ink">{PROVIDER_LABELS[provider.alias] ?? provider.alias}</p>
                <p className="text-[11px] text-ink-faint">
                  {provider.configured ? "key set" : "no key"}
                  {provider.needs_base_url && " · needs base URL"}
                </p>
              </div>
              <Button variant="ghost" onClick={() => setEditing(provider)}>
                <KeyRound size={14} />
                {provider.configured ? "Replace" : "Add key"}
              </Button>
              {provider.configured && (
                <Button
                  variant="ghost"
                  aria-label={`Remove ${provider.alias} key`}
                  onClick={() => clear.mutate(provider.alias)}
                >
                  <Trash2 size={14} />
                </Button>
              )}
            </div>
          ))}
      </div>
      <KeySheet provider={editing} onClose={() => setEditing(null)} />
    </Card>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

export function ModelsScreen() {
  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const installed = new Set((models.data ?? []).map((m) => m.name));

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <h1 className="font-serif text-xl text-ink">Models</h1>
        <CatalogBrowser installed={installed} />
        <DownloadTray />
        <LocalModels />
        <EmbedDefault />
        <Providers />
      </div>
    </div>
  );
}
