/**
 * Models — the model manager. Local models (pull with live progress, delete,
 * loaded state, hide from pickers, set as global default) and hosted providers
 * (key entry → core → OpenBao; the key never comes back).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  Cpu,
  Eye,
  EyeOff,
  KeyRound,
  MemoryStick,
  RefreshCw,
  Search,
  Sparkles,
  SlidersHorizontal,
  Star,
  Trash2,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import { useState, type ReactNode } from "react";

import {
  Badge,
  Button,
  Card,
  Confirm,
  Dot,
  Label,
  Select,
  Sheet,
  Spinner,
  TextInput,
  Tooltip,
  cn,
} from "@/components/ui";
import { ALL_TAGS, CATALOG, TAG_LABELS, filterCatalog, formatGb, type CatalogTag } from "@/data/catalog";
import { api } from "@/lib/api";
import { PROVIDER_LABELS, PROVIDER_MODEL_HINTS, formatBytes, relativeTime } from "@/lib/format";
import type { ProviderInfo, SavedHostedModel, SystemInfo } from "@/lib/contracts";
import { CAPABILITY_META, shownCapabilities } from "@/lib/icons";
import { assessFit, fitFilterOf, type FitFilter } from "@/lib/modelFit";
import { recommendKvCache } from "@/lib/kvCacheFit";
import {
  formatVariantSize,
  isCloudTag,
  recommendVariantTag,
  sortVariants,
  variantSizeMb,
} from "@/lib/quantVariants";
import { useDownloads } from "@/stores/downloads";

// ── "Good for your system?" status icon ─────────────────────────────────────────

/** A fit verdict's tone → its compact status icon. `dim` (the `unknown` rating) maps to
 *  nothing, so a model with no verdict stays clean. */
const FIT_ICON = { ok: Check, warn: TriangleAlert, danger: X, dim: null } as const;
const FIT_ICON_TONE = {
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
  dim: "text-ink-faint",
} as const;

/**
 * A suitability **status icon** — check (fits), warning triangle (tight / offloads / heavy),
 * or X (too big) — with the full label + reason on hover/tap via native `title` (so it works
 * on touch). Replaces the old text chip, which ate horizontal space and crowded the row on a
 * phone (#327). `sizeMb` is a known size; pass null + the `params` label to estimate (catalog).
 * Renders nothing when there's no verdict.
 */
export function FitBadge({
  system,
  sizeMb,
  params,
}: {
  system: SystemInfo | undefined;
  sizeMb: number | null;
  params?: string | null;
}) {
  const fit = assessFit(system, sizeMb, params);
  const Icon = FIT_ICON[fit.tone];
  if (!fit.label || !Icon) return null;
  return (
    <span
      title={`${fit.label} — ${fit.reason}`}
      role="img"
      aria-label={`Suitability: ${fit.label}`}
      className={cn("inline-flex cursor-help items-center", FIT_ICON_TONE[fit.tone])}
    >
      <Icon size={15} className="shrink-0" aria-hidden="true" />
    </span>
  );
}

// ── Capability icons ────────────────────────────────────────────────────────────

/**
 * A model's capabilities (tools / vision / thinking / embedding) as **icon-only** glyphs, each
 * with a hover/focus tooltip carrying its label. Dropping the text keeps a row of them compact on
 * a phone (#384) — mirroring the suitability status-icon (#327) and the chat activity-badge
 * (#334), both icon-with-tooltip. Renders nothing when the model reports no badge-worthy
 * capability. Shared by the local-model rows and the quant-variant settings panel (#385).
 */
export function CapabilityIcons({
  capabilities,
  size = 13,
}: {
  capabilities: string[];
  size?: number;
}) {
  const shown = shownCapabilities(capabilities);
  if (shown.length === 0) return null;
  return (
    <span className="inline-flex items-center gap-1">
      {shown.map((cap) => {
        const Icon = CAPABILITY_META[cap].icon;
        return (
          <Tooltip key={cap} label={CAPABILITY_META[cap].label}>
            <span role="img" aria-label={CAPABILITY_META[cap].label} className="inline-flex text-ink-faint">
              <Icon size={size} className="shrink-0" aria-hidden="true" />
            </span>
          </Tooltip>
        );
      })}
    </span>
  );
}

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

/** Return a new set with `value` toggled — add if absent, remove if present. Immutable so React
 *  sees a fresh reference and the chips re-render. */
function toggled<T>(set: ReadonlySet<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

/** The catalog's fit-filter chips (#388), worst-fit last. Each `key` is a `FitFilter` bucket; the
 *  glyph mirrors the row's suitability status-icon (check / warning / cross). */
const FIT_FILTERS: { key: FitFilter; label: string; icon: LucideIcon }[] = [
  { key: "ok", label: "Fits", icon: Check },
  { key: "warn", label: "Tight", icon: TriangleAlert },
  { key: "danger", label: "Too big", icon: X },
];

/** A pill toggle shared by the tag and fit catalog filters. */
function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-3 py-1 text-xs transition-colors",
        active
          ? "border-accent bg-accent-dim text-accent-strong"
          : "border-edge text-ink-dim hover:border-edge-strong hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

export function CatalogBrowser({ installed }: { installed: Set<string> }) {
  const queryClient = useQueryClient();
  const pull = useDownloads((s) => s.pull);
  const active = useDownloads((s) => s.active);
  const [query, setQuery] = useState("");
  // Multi-select filters: a model must carry *all* checked tags (#389). Fit filters (#388) are
  // OR'd among themselves (a model has exactly one fit) and AND'd with the tag set. An empty set
  // means "no filter" for that dimension.
  const [activeTags, setActiveTags] = useState<ReadonlySet<CatalogTag>>(new Set());
  const [activeFits, setActiveFits] = useState<ReadonlySet<FitFilter>>(new Set());

  // The live list comes from the core, which parses it from upstream on a schedule (#269).
  // Fall back to the bundled seed when that endpoint is unreachable (e.g. an older core),
  // so the browser is never empty.
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: api.catalog });
  const system = useQuery({ queryKey: ["systemInfo"], queryFn: api.systemInfo });
  const source = catalog.data;

  // Tags + search first (pure), then the fit filter — fit is computed client-side from the
  // detected system (it's not a catalog tag), so it lives here rather than in `filterCatalog`.
  const tagged = filterCatalog(source?.entries ?? CATALOG, query, activeTags);
  const entries =
    activeFits.size === 0
      ? tagged
      : tagged.filter((e) => {
          // Cloud-only rows have no local weights: excluded from fit by design (#571).
          if (e.tags.includes("cloud")) return false;
          const sizeMb = e.size_gb != null ? Math.round(e.size_gb * 1024) : null;
          const bucket = fitFilterOf(assessFit(system.data, sizeMb, e.params));
          return bucket !== null && activeFits.has(bucket);
        });

  const startPull = (id: string) => {
    void pull(id, () => queryClient.invalidateQueries({ queryKey: ["models"] }));
  };

  return (
    <Card>
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <h3 className="font-serif text-base text-ink">Browse models</h3>
      </div>
      <p className="mb-3 text-[11px] text-ink-faint">
        {catalog.isError || source?.stale
          ? "Showing the built-in list — couldn't reach the model library."
          : source
            ? `From ${source.source.replace(/^https?:\/\//, "")}${
                source.updated_at ? ` · updated ${relativeTime(source.updated_at)}` : ""
              }`
            : "Loading the latest models…"}
      </p>

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

      {/* Filters — a row of multi-select tag chips ("All" clears them), then, once the system is
          known, a row of fit chips (#388/#389). */}
      <div className="mb-4 flex flex-col gap-2">
        <div className="flex flex-wrap gap-1.5">
          <FilterChip active={activeTags.size === 0} onClick={() => setActiveTags(new Set())}>
            All
          </FilterChip>
          {ALL_TAGS.map((tag) => (
            <FilterChip
              key={tag}
              active={activeTags.has(tag)}
              onClick={() => setActiveTags((prev) => toggled(prev, tag))}
            >
              {TAG_LABELS[tag]}
            </FilterChip>
          ))}
        </div>
        {system.data && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-0.5 text-[11px] uppercase tracking-wide text-ink-faint">Fit</span>
            {FIT_FILTERS.map(({ key, label, icon: Icon }) => (
              <FilterChip
                key={key}
                active={activeFits.has(key)}
                onClick={() => setActiveFits((prev) => toggled(prev, key))}
              >
                <Icon size={12} className="shrink-0" aria-hidden="true" />
                {label}
              </FilterChip>
            ))}
          </div>
        )}
      </div>

      {/* Entries — capped to roughly five rows tall with its own scroll so the full
          catalog (dozens of models) never pushes the rest of the page away. The search
          and tag filters above stay put; only this list scrolls. */}
      {entries.length === 0 ? (
        <p className="py-4 text-center text-sm text-ink-dim">No models match your search.</p>
      ) : (
        <div className="flex max-h-[30rem] flex-col divide-y divide-edge overflow-y-auto overscroll-contain">
          {entries.map((entry) => {
            const dl = active[entry.id];
            const inProgress = dl && !dl.done;
            const isInstalled = installed.has(entry.id);
            // No local weights: badge instead of Pull, no fit verdict — by design (#571).
            const isCloud = entry.tags.includes("cloud");

            return (
              <div key={entry.id} className="flex items-start gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-sm text-ink">{entry.id}</span>
                    {entry.params && <Badge tone="dim">{entry.params}</Badge>}
                    {entry.size_gb != null && (
                      <span className="text-xs text-ink-faint">{formatGb(entry.size_gb)}</span>
                    )}
                    {!isCloud && (
                      <FitBadge
                        system={system.data}
                        sizeMb={entry.size_gb != null ? Math.round(entry.size_gb * 1024) : null}
                        params={entry.params}
                      />
                    )}
                    {entry.pulls && <span className="text-xs text-ink-faint">{entry.pulls} pulls</span>}
                  </div>
                  <p className="mt-0.5 text-xs leading-relaxed text-ink-dim">{entry.description}</p>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {entry.tags.map((t) => (
                      <span
                        key={t}
                        className="rounded-full bg-surface-2 px-2 py-0.5 text-[10px] text-ink-faint"
                      >
                        {TAG_LABELS[t as CatalogTag] ?? t}
                      </span>
                    ))}
                  </div>
                </div>

                <div className="shrink-0 pt-0.5">
                  {isInstalled ? (
                    <Badge tone="ok">Installed</Badge>
                  ) : isCloud ? (
                    /* Native title (not the hover Tooltip) so the reason is reachable on
                       touch, mirroring the FitBadge (#327). */
                    <span
                      title="Runs on the model library's cloud — there are no local weights to download, so size, fit, and Pull don't apply."
                      className="inline-flex cursor-help"
                    >
                      <Badge tone="warn">cloud-only</Badge>
                    </span>
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

export function LocalModels() {
  const queryClient = useQueryClient();
  // Ask for capabilities here so each model can be badged with what it does (tools/vision/…).
  // Keyed under ["models", …] so the mutations' `["models"]` invalidation still refreshes it.
  const models = useQuery({
    queryKey: ["models", "capabilities"],
    queryFn: () => api.models(true),
    // Keep the loaded badge live on the PWA (#331): poll while the page is visible and refetch
    // when the tab regains focus, so unloading on another device shows up here without a reload.
    refetchInterval: 10_000,
    refetchOnWindowFocus: true,
  });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const system = useQuery({ queryKey: ["systemInfo"], queryFn: api.systemInfo });
  const [confirming, setConfirming] = useState<string | null>(null);
  // Which model's inline settings panel is open. One at a time (accordion); tapping the row
  // toggles it. Replaces the old hover-only buttons + settings Sheet so the row never overflows
  // on a phone and every control is reachable by touch (#328).
  const [expanded, setExpanded] = useState<string | null>(null);

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

  // Unload from memory now (keep_alive=0), without touching power state (#331). `null` = all.
  const unload = useMutation({
    mutationFn: (model: string | null) => api.unloadModel(model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  });

  const anyLoaded = (models.data ?? []).some((m) => m.loaded);

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="font-serif text-base text-ink">Local models</h3>
        {anyLoaded && (
          <Button
            variant="ghost"
            onClick={() => unload.mutate(null)}
            disabled={unload.isPending}
            aria-label="Unload all models from memory"
          >
            <MemoryStick size={14} />
            Unload all
          </Button>
        )}
      </div>
      {models.isLoading && <Spinner />}
      {models.isError && (
        <p className="text-sm text-warn">
          The local runtime is unreachable — is the ollama service up?
        </p>
      )}
      {models.data?.length === 0 && (
        <p className="text-sm text-ink-dim">None yet. Pull one above — it stays on your disk.</p>
      )}
      <div className="flex flex-col gap-1.5">
        {models.data?.map((model) => {
          const isDefault = globalDefault === model.name;
          const isOpen = expanded === model.name;
          return (
            <div
              key={model.name}
              className={cn(
                "rounded-(--radius-field) border transition-colors",
                isOpen ? "border-edge bg-surface-2" : "border-transparent",
                model.hidden && "opacity-60",
              )}
            >
              {/* The whole row is the disclosure toggle — large touch target, no hover-only
                  controls. Name + status badges + suitability + capabilities + size + chevron. */}
              <button
                type="button"
                aria-expanded={isOpen}
                aria-label={`${isOpen ? "Hide" : "Show"} settings for ${model.name}`}
                onClick={() => setExpanded(isOpen ? null : model.name)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-(--radius-field) px-2 py-2 text-left",
                  !isOpen && "hover:bg-surface-2",
                )}
              >
                <ChevronRight
                  size={15}
                  className={cn(
                    "shrink-0 text-ink-faint transition-transform",
                    isOpen && "rotate-90",
                  )}
                />
                <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="truncate text-sm text-ink">{model.name}</span>
                  {model.loaded && <Badge tone="ok">loaded</Badge>}
                  {isDefault && <Badge tone="accent">default</Badge>}
                  {model.hidden && <Badge tone="dim">hidden</Badge>}
                  <FitBadge
                    system={system.data}
                    sizeMb={model.size ? Math.round(model.size / (1024 * 1024)) : null}
                  />
                  <CapabilityIcons capabilities={model.capabilities} />
                  {model.context_length != null && (
                    <Tooltip label={`${model.context_length.toLocaleString()} token context`}>
                      <Badge tone="dim">{formatContextLength(model.context_length)}</Badge>
                    </Tooltip>
                  )}
                </div>
                <span className="shrink-0 text-xs text-ink-faint">{formatBytes(model.size)}</span>
              </button>

              {/* Inline settings panel — actions live here (always visible, touch-friendly) plus
                  the per-model context window / keep-alive / run-on form. */}
              {isOpen && (
                <div className="border-t border-edge px-3 pb-4 pt-3">
                  <div className="mb-4 flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      onClick={() => setDefault.mutate(isDefault ? null : model.name)}
                      disabled={setDefault.isPending}
                    >
                      <Star size={14} fill={isDefault ? "currentColor" : "none"} />
                      {isDefault ? "Default model" : "Set as default"}
                    </Button>
                    {model.loaded && (
                      <Button
                        variant="outline"
                        onClick={() => unload.mutate(model.name)}
                        disabled={unload.isPending}
                      >
                        <MemoryStick size={14} />
                        Unload
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      onClick={() => toggleHidden.mutate({ name: model.name, hidden: !model.hidden })}
                      disabled={toggleHidden.isPending}
                    >
                      {model.hidden ? <Eye size={14} /> : <EyeOff size={14} />}
                      {model.hidden ? "Show in pickers" : "Hide from pickers"}
                    </Button>
                    <Button variant="danger" onClick={() => setConfirming(model.name)}>
                      <Trash2 size={14} />
                      Delete
                    </Button>
                  </div>
                  <ModelSettingsForm model={model.name} />
                </div>
              )}
            </div>
          );
        })}
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

// ── Context window ──────────────────────────────────────────────────────────────

/** Render a megabyte count as a compact GB string (VRAM / model size). */
function formatMb(mb: number | null | undefined): string {
  if (mb == null || mb <= 0) return "—";
  return `${(mb / 1024).toFixed(mb < 10 * 1024 ? 1 : 0)} GB`;
}

/** Render a token count as a compact "128k"/"200k"/"1M" chip (#618) — decimal-based (a
 *  provider's advertised "128k" context is conventionally 128,000, not the binary 131,072). */
function formatContextLength(n: number): string {
  if (n < 1_000) return String(n);
  const unit = n >= 1_000_000 ? 1_000_000 : 1_000;
  const value = Math.round((n / unit) * 10) / 10;
  return `${value}${unit === 1_000_000 ? "M" : "k"}`;
}

const CTX_FLOOR = 2048;
const CTX_CEILING = 32768;
const CTX_STEP = 512;

/**
 * Context window — how many tokens the local runtime keeps in play (Ollama `num_ctx`).
 * The default 4096 is small enough that epicurus's own system prompt (instructions + every
 * module's tool schemas) can fill it, leaving no room to answer. This card surfaces a
 * hardware-aware suggestion and lets the operator set the persisted pref.
 */
export function ContextWindow() {
  const queryClient = useQueryClient();
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const system = useQuery({ queryKey: ["systemInfo"], queryFn: api.systemInfo });

  const stored = llmPrefs.data?.global_context_window ?? null;
  const suggestion = system.data?.suggested_context ?? null;
  const gpu = system.data?.gpu ?? null;
  const cpu = system.data?.cpu ?? null;
  const ram = system.data?.ram_total_mb ?? null;
  const model = system.data?.model ?? null;
  const kvCacheType = system.data?.kv_cache_type ?? null;

  // The in-progress edit. `undefined` = untouched (show the stored/suggested value);
  // a number = the operator is editing; deriving the displayed value (rather than seeding
  // state in an effect) keeps it in sync with the pref without cascading renders.
  const [draft, setDraft] = useState<number | undefined>(undefined);

  const save = useMutation({
    mutationFn: (value: number | null) => api.setContextWindow(value),
    onSuccess: () => {
      setDraft(undefined); // fall back to following the freshly-saved pref
      void queryClient.invalidateQueries({ queryKey: ["llmPrefs"] });
    },
  });

  const value = draft ?? stored ?? suggestion?.suggested ?? 4096;
  const sliderMax = Math.max(suggestion?.max ?? CTX_CEILING, value, CTX_FLOOR);
  const dirty = draft !== undefined && draft !== stored;

  const commit = (next: number | null) => save.mutate(next);
  const clampToStep = (raw: number) =>
    Math.min(sliderMax, Math.max(CTX_FLOOR, Math.round(raw / CTX_STEP) * CTX_STEP));

  return (
    <Card>
      <h3 className="mb-1 font-serif text-base text-ink">Default context window</h3>
      <p className="mb-3 text-xs leading-relaxed text-ink-dim">
        How many tokens the local runtime keeps in play (Ollama <code>num_ctx</code>). The
        agent's instructions and tool list alone are sizeable — too small a window and there's
        no room left to answer. This is the default for every local model; expand a model above
        to override it for that model alone.
      </p>

      {/* detected hardware + active model */}
      <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2">
          <p className="text-[11px] uppercase tracking-wide text-ink-faint">Your system</p>
          {system.isLoading ? (
            <Spinner className="mt-1" />
          ) : (
            <div className="mt-0.5 flex flex-col gap-0.5 text-sm text-ink">
              {gpu ? (
                <span className="flex items-center gap-1.5">
                  <Cpu size={14} className="shrink-0 text-accent" />
                  <span className="truncate">
                    {gpu.name} · {formatMb(gpu.vram_total_mb)} VRAM
                  </span>
                </span>
              ) : (
                <span className="flex items-center gap-1.5">
                  <Cpu size={14} className="shrink-0 text-ink-faint" />
                  No GPU — CPU inference
                </span>
              )}
              {cpu && (
                <span className="truncate text-xs text-ink-dim">
                  {cpu.model}
                  {cpu.physical_cores ? ` · ${cpu.physical_cores} cores` : ""}
                </span>
              )}
              {ram && <span className="text-xs text-ink-dim">{formatMb(ram)} RAM</span>}
            </div>
          )}
        </div>
        <div className="rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2">
          <p className="text-[11px] uppercase tracking-wide text-ink-faint">Active model</p>
          <p className="mt-0.5 truncate text-sm text-ink">
            {model ? model.name : "—"}
            {model?.size_mb ? ` · ${formatMb(model.size_mb)}` : ""}
          </p>
          {model && (model.quantization || model.context_length) && (
            <p className="mt-0.5 truncate text-xs text-ink-dim">
              {[
                model.quantization,
                model.context_length
                  ? `trained ${model.context_length.toLocaleString()} ctx`
                  : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
        </div>
      </div>

      {/* suggestion */}
      {suggestion && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-(--radius-field) border border-accent/30 bg-accent-dim/40 px-3 py-2 text-sm">
          <Sparkles size={14} className="shrink-0 text-accent" />
          <span className="text-ink">
            Suggested <strong>{suggestion.suggested.toLocaleString()}</strong>
            <span className="text-ink-dim">
              {" "}
              (range {suggestion.min.toLocaleString()}–{suggestion.max.toLocaleString()})
            </span>
          </span>
          <Button
            variant="outline"
            className="ml-auto"
            onClick={() => commit(suggestion.suggested)}
            disabled={save.isPending}
          >
            Use suggested ({suggestion.suggested.toLocaleString()})
          </Button>
        </div>
      )}
      <p className="mb-3 text-[11px] italic leading-relaxed text-ink-faint">
        The suggestion is a rough estimate from your VRAM, the model's size, and its trained
        context limit
        {kvCacheType ? `, with your ${kvCacheType} KV cache factored in` : ""} — a sensible
        starting point, not a measured maximum. Tune it if replies run short or the runtime
        complains.
      </p>

      {/* number input + slider bound to the pref */}
      {llmPrefs.isLoading ? (
        <Spinner />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <Label>Tokens</Label>
            <TextInput
              type="number"
              min={CTX_FLOOR}
              max={sliderMax}
              step={CTX_STEP}
              value={value}
              aria-label="Context window tokens"
              className="w-32"
              disabled={save.isPending}
              onChange={(e) => setDraft(e.target.value ? Number(e.target.value) : undefined)}
              onBlur={() => draft !== undefined && commit(clampToStep(draft))}
            />
            {stored !== null && (
              <Button
                variant="ghost"
                onClick={() => commit(null)}
                disabled={save.isPending}
                aria-label="Reset to the system default"
              >
                Reset to default
              </Button>
            )}
          </div>
          {/* eslint-disable-next-line no-restricted-syntax -- range slider, not a styled text field */}
          <input
            type="range"
            min={CTX_FLOOR}
            max={sliderMax}
            step={CTX_STEP}
            value={Math.min(value, sliderMax)}
            aria-label="Context window slider"
            className="w-full accent-accent"
            disabled={save.isPending}
            onChange={(e) => setDraft(Number(e.target.value))}
            onPointerUp={() => draft !== undefined && commit(clampToStep(draft))}
          />
          <p className="text-xs text-ink-dim">
            {stored === null
              ? "Using the system default — set a value to override it."
              : dirty
                ? "Unsaved — release the slider or leave the field to apply."
                : `Saved · the runtime will use ${stored.toLocaleString()} tokens.`}
          </p>
          {save.isError && (
            <p className="text-sm text-danger">{(save.error as Error).message}</p>
          )}
        </div>
      )}
    </Card>
  );
}

// ── Per-model settings ──────────────────────────────────────────────────────────

/**
 * Model settings — per-model tuning for any local model (chat or embedding). Context window
 * and keep-alive are live runtime knobs (resolved per model: this value → the global default →
 * the env). Quantization is read-only — it's baked in when the model is pulled, so changing it
 * means pulling a different variant, which this form offers as a shortcut.
 *
 * The body is shared: it renders inline inside each expanded model row (#328) and inside the
 * embedding-default Sheet. The component mounts fresh per model (the row unmounts on collapse,
 * the Sheet on close), so seeding the draft is a one-shot boolean guard — no cross-model reset
 * dance. `onSaved` lets the Sheet close on save; inline it's omitted so the panel stays open.
 */
export function ModelSettingsForm({ model, onSaved }: { model: string; onSaved?: () => void }) {
  const queryClient = useQueryClient();
  const pull = useDownloads((s) => s.pull);
  const settings = useQuery({
    queryKey: ["modelSettings", model],
    queryFn: () => api.modelSettings(model),
  });
  const details = useQuery({
    queryKey: ["modelDetails", model],
    queryFn: () => api.modelDetails(model),
  });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const system = useQuery({ queryKey: ["systemInfo"], queryFn: api.systemInfo });
  // Quant variants for this model, looked up on demand from the registry (#330). Best-effort:
  // an empty list (offline / non-library model) just hides the pick-list and keeps the manual box.
  const variantsQuery = useQuery({
    queryKey: ["modelVariants", model],
    queryFn: () => api.modelVariants(model),
  });

  // Draft form state, seeded once when the per-model settings arrive
  // (adjust-state-during-render, not an effect — the React-recommended pattern).
  const [ctx, setCtx] = useState("");
  const [keepAlive, setKeepAlive] = useState("");
  const [device, setDevice] = useState<string>(""); // "" = auto
  const [variant, setVariant] = useState(() => model.split(":")[0]);
  const [seeded, setSeeded] = useState(false);

  if (settings.data && !seeded) {
    setCtx(settings.data.context_window != null ? String(settings.data.context_window) : "");
    setKeepAlive(settings.data.keep_alive ?? "");
    setDevice(settings.data.device ?? "");
    setSeeded(true);
  }

  const save = useMutation({
    mutationFn: () =>
      api.setModelSettings(model, {
        context_window: ctx.trim() === "" ? null : Number(ctx),
        keep_alive: keepAlive.trim() === "" ? null : keepAlive.trim(),
        device: device || null,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["modelSettings", model] });
      onSaved?.();
    },
  });

  const trainedMax = details.data?.context_length ?? null;
  // The context this model will actually use, resolved the way the gateway does it: this model's
  // own value → the global default → the system suggestion → Ollama's 4096. Drives a live
  // read-out so the operator always sees the effective number, never a blank "inherit" (#328).
  const ctxNum = ctx.trim() === "" ? null : Number(ctx);
  const globalCtx = llmPrefs.data?.global_context_window ?? null;
  const suggestedCtx = system.data?.suggested_context?.suggested ?? null;
  const inheritedCtx = globalCtx ?? suggestedCtx ?? 4096;
  const effectiveCtx = ctxNum ?? inheritedCtx;
  const sliderMax = Math.max(CTX_CEILING, trainedMax ?? 0, effectiveCtx);

  const startPull = () => {
    const tag = variant.trim();
    if (!tag) return;
    void pull(tag, () => queryClient.invalidateQueries({ queryKey: ["models"] }));
    onSaved?.();
  };

  // Pull a variant from the pick-list — keep the panel open so the download tray shows progress.
  const pullVariant = (tag: string) => {
    void pull(tag, () => queryClient.invalidateQueries({ queryKey: ["models"] }));
  };

  const paramSize = details.data?.parameter_size;
  const variantList = sortVariants(variantsQuery.data?.variants ?? []);
  const recommendedTag = recommendVariantTag(
    variantsQuery.data?.variants ?? [],
    paramSize,
    system.data,
  );

  const hasOverrides =
    settings.data?.context_window != null ||
    !!settings.data?.keep_alive ||
    !!settings.data?.device;

  return (
    <div className="flex flex-col gap-5">
      {/* read-only facts from the runtime — quant + size + trained ctx, plus what the model can do.
          Capabilities are a model-level fact (they don't vary by quant), so they sit here once
          rather than repeating on every variant row below (#385). */}
      <div className="flex flex-wrap items-center gap-1.5">
        {details.isLoading ? (
          <Spinner />
        ) : (
          <>
            {details.data?.quantization && <Badge tone="dim">{details.data.quantization}</Badge>}
            {details.data?.parameter_size && (
              <Badge tone="dim">{details.data.parameter_size}</Badge>
            )}
            {trainedMax != null && (
              <Badge tone="dim">trained {trainedMax.toLocaleString()} ctx</Badge>
            )}
            <CapabilityIcons capabilities={details.data?.capabilities ?? []} />
          </>
        )}
      </div>

      {/* context window — per-model, with a live readout of the resolved value */}
      <div>
        <Label hint="Ollama num_ctx for this model. Leave blank to inherit the global default.">
          Context window
        </Label>
        <div className="flex items-center gap-3">
          <TextInput
            type="number"
            min={CTX_FLOOR}
            max={trainedMax ?? CTX_CEILING}
            step={CTX_STEP}
            value={ctx}
            placeholder={String(inheritedCtx)}
            aria-label="Per-model context window tokens"
            className="w-32"
            onChange={(e) => setCtx(e.target.value)}
          />
          {/* eslint-disable-next-line no-restricted-syntax -- range slider, not a styled text field */}
          <input
            type="range"
            min={CTX_FLOOR}
            max={sliderMax}
            step={CTX_STEP}
            value={Math.min(Math.max(effectiveCtx, CTX_FLOOR), sliderMax)}
            aria-label="Per-model context window slider"
            className="flex-1 accent-accent"
            onChange={(e) => setCtx(e.target.value)}
          />
        </div>
        <p className="mt-1.5 text-xs text-ink-dim">
          {ctxNum != null ? (
            <>
              This model will use <strong>{ctxNum.toLocaleString()}</strong> tokens.
            </>
          ) : (
            <>
              Inherits <strong>{inheritedCtx.toLocaleString()}</strong> tokens from{" "}
              {globalCtx != null ? "the global default" : "the system suggestion"}.
            </>
          )}
        </p>
      </div>

      {/* keep-alive */}
      <div>
        <Label hint="How long the runtime keeps this model loaded after use — e.g. 5m, 30m, 0 (unload now), -1 (forever). Blank inherits the default.">
          Keep-alive
        </Label>
        <TextInput
          value={keepAlive}
          placeholder="inherit"
          aria-label="Keep-alive"
          className="w-40"
          onChange={(e) => setKeepAlive(e.target.value)}
        />
      </div>

      {/* run on: GPU / CPU / auto */}
      <div>
        <Label hint="Where this model runs. Auto lets the runtime decide; GPU offloads all layers; CPU keeps it off the GPU. Local models only.">
          Run on
        </Label>
        <div className="flex gap-1.5" role="group" aria-label="Run on">
          {[
            { value: "", label: "Auto" },
            { value: "gpu", label: "GPU" },
            { value: "cpu", label: "CPU" },
          ].map((opt) => (
            <button
              key={opt.value || "auto"}
              type="button"
              aria-pressed={device === opt.value}
              onClick={() => setDevice(opt.value)}
              className={cn(
                "rounded-full border px-4 py-1 text-xs transition-colors",
                device === opt.value
                  ? "border-accent bg-accent-dim text-accent-strong"
                  : "border-edge text-ink-dim hover:border-edge-strong hover:text-ink",
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {save.isError && <p className="text-sm text-danger">{(save.error as Error).message}</p>}
      <div className="flex items-center gap-2">
        <Button variant="primary" busy={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
        {hasOverrides && (
          <Button
            variant="ghost"
            disabled={save.isPending}
            onClick={() => {
              setCtx("");
              setKeepAlive("");
              setDevice("");
              save.mutate();
            }}
          >
            Reset to defaults
          </Button>
        )}
      </div>

      {/* quantization — read-only fact + a pick-list of available variants + manual fallback */}
      <div className="border-t border-edge pt-4">
        <Label hint="Quantization is fixed when a model is pulled. Pick another variant below to download it alongside this one — a smaller quant frees VRAM, a larger one keeps more quality.">
          Quantization
        </Label>
        <p className="mb-2 text-sm text-ink">
          {details.data?.quantization ?? "unknown"}
          <span className="text-ink-faint"> · read-only (this model)</span>
        </p>

        {/* available variants from the registry (#330) */}
        {variantsQuery.isLoading ? (
          <div className="mb-3">
            <Spinner />
          </div>
        ) : variantList.length > 0 ? (
          <div className="mb-3 flex max-h-56 flex-col divide-y divide-edge overflow-y-auto overscroll-contain rounded-(--radius-field) border border-edge">
            {variantList.map((v) => {
              const installed = v.tag === model;
              const recommended = v.tag === recommendedTag;
              // Real tags-page size when the core supplied one (#571); estimate otherwise.
              const sizeMb = variantSizeMb(v, paramSize);
              return (
                <div key={v.tag} className="flex items-center gap-2 px-2.5 py-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-mono text-xs text-ink">
                        {v.quant || (isCloudTag(v.tag) ? "cloud" : "default")}
                      </span>
                      {/* Per-variant fit (#385): each quant's size — real when known (#571),
                          estimated otherwise — judged against the detected hardware, so a
                          smaller quant can read "Fits" where the default build is "Tight".
                          Renders nothing when the size is unknown (cloud aliases). */}
                      <FitBadge system={system.data} sizeMb={sizeMb} />
                      {recommended && (
                        <Badge tone="accent">
                          <Sparkles size={10} className="shrink-0" /> recommended
                        </Badge>
                      )}
                      {installed && <Badge tone="ok">installed</Badge>}
                    </div>
                    <p className="truncate font-mono text-[10px] text-ink-faint">
                      {v.tag}
                      {sizeMb != null
                        ? ` · ${v.size_gb != null ? formatGb(v.size_gb) : formatVariantSize(sizeMb)}`
                        : ""}
                    </p>
                  </div>
                  {installed ? (
                    <span className="shrink-0 text-[11px] text-ink-faint">current</span>
                  ) : (
                    <Button variant="outline" onClick={() => pullVariant(v.tag)}>
                      Pull
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        ) : null}

        {/* manual fallback — a specific tag (e.g. a non-library or HF model) */}
        <div className="flex items-center gap-2">
          <TextInput
            value={variant}
            aria-label="Variant tag to pull"
            placeholder="model:tag"
            className="flex-1 font-mono"
            onChange={(e) => setVariant(e.target.value)}
          />
          <Button variant="outline" onClick={startPull} disabled={!variant.trim()}>
            Pull variant
          </Button>
        </div>
      </div>
    </div>
  );
}

/**
 * The embedding-default card still opens settings in a Sheet (it has no row to expand); the
 * body is the same `ModelSettingsForm` the inline per-model panels use.
 */
export function ModelSettingsSheet({
  model,
  onClose,
}: {
  model: string | null;
  onClose: () => void;
}) {
  if (model === null) return null;
  return (
    <Sheet open onClose={onClose} title="Model settings">
      <p className="-mt-1 mb-4 font-mono text-sm break-all text-ink">{model}</p>
      <ModelSettingsForm model={model} onSaved={onClose} />
    </Sheet>
  );
}

/**
 * Settings for a saved **hosted** model — the context-window field only (#570).
 *
 * On a hosted model the number is a *compaction budget*, not an Ollama `num_ctx`: the provider
 * fixes the real window, so this caps how large a conversation we send (it's trimmed to fit before
 * each request), which both averts the provider's over-window rejection and bounds per-turn input
 * spend. Keep-alive, Run-on, and quantization are Ollama runtime concerns and stay hidden here; the
 * global Ollama context pref never applies to a hosted call, so there is no "inherit" read-out —
 * blank simply means no budget (send the whole conversation, up to the provider's own window).
 */
function HostedModelSettingsForm({ model, onSaved }: { model: string; onSaved: () => void }) {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["modelSettings", model],
    queryFn: () => api.modelSettings(model),
  });

  // Seeded once when the stored settings arrive (adjust-state-during-render, per the local form).
  const [ctx, setCtx] = useState("");
  const [seeded, setSeeded] = useState(false);
  if (settings.data && !seeded) {
    setCtx(settings.data.context_window != null ? String(settings.data.context_window) : "");
    setSeeded(true);
  }

  const save = useMutation({
    // The budget is passed in explicitly (not read from `ctx` state) so "Clear" can save null in
    // the same click without waiting for the state update to settle. keep_alive and device are
    // local-only runtime options — always cleared for a hosted model.
    mutationFn: (value: number | null) =>
      api.setModelSettings(model, { context_window: value, keep_alive: null, device: null }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["modelSettings", model] });
      onSaved();
    },
  });

  const ctxNum = ctx.trim() === "" ? null : Number(ctx);
  const hasBudget = settings.data?.context_window != null;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <Label hint="Compaction budget — a long conversation is trimmed to fit this many tokens before each request, so it can't overflow the provider's window. Also caps per-turn input spend. Leave blank to send the whole conversation.">
          Context window
        </Label>
        <TextInput
          type="number"
          min={CTX_FLOOR}
          step={CTX_STEP}
          value={ctx}
          placeholder="no budget"
          aria-label="Hosted context window tokens"
          className="w-40"
          onChange={(e) => setCtx(e.target.value)}
        />
        <p className="mt-1.5 text-xs text-ink-dim">
          {ctxNum != null ? (
            <>
              Conversations are trimmed to <strong>{ctxNum.toLocaleString()}</strong> tokens before
              each request.
            </>
          ) : (
            <>No budget set — the whole conversation is sent, up to the provider&apos;s own window.</>
          )}
        </p>
      </div>

      {save.isError && <p className="text-sm text-danger">{(save.error as Error).message}</p>}
      <div className="flex items-center gap-2">
        <Button variant="primary" busy={save.isPending} onClick={() => save.mutate(ctxNum)}>
          Save
        </Button>
        {hasBudget && (
          <Button
            variant="ghost"
            disabled={save.isPending}
            onClick={() => {
              setCtx("");
              save.mutate(null);
            }}
          >
            Clear budget
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * The settings Sheet for a saved hosted model — the hosted analog of `ModelSettingsSheet`, showing
 * the context field only (a compaction budget, #570). Renders nothing when no model is selected.
 */
export function HostedModelSettingsSheet({
  model,
  onClose,
}: {
  model: string | null;
  onClose: () => void;
}) {
  if (model === null) return null;
  return (
    <Sheet open onClose={onClose} title="Hosted model settings">
      <p className="-mt-1 mb-4 font-mono text-sm break-all text-ink">{model}</p>
      <HostedModelSettingsForm model={model} onSaved={onClose} />
    </Sheet>
  );
}

// ── KV-cache type (global runtime setting) ──────────────────────────────────────

const KV_CACHE_OPTIONS = [
  { value: "", label: "Default (f16)" },
  { value: "q8_0", label: "q8_0 — half the cache VRAM" },
  { value: "q4_0", label: "q4_0 — quarter the cache VRAM" },
];

/**
 * KV-cache type — quantizes the attention cache to fit a longer context in less VRAM. It's a
 * **server-wide** Ollama start flag (and q8_0/q4_0 need flash attention). Picking one persists
 * the choice and, when Docker is wired, the core writes Ollama's env file and restarts it to
 * apply (#307) — flash attention enabled automatically. If the core can't reach Docker it falls
 * back to spelling out the manual env + restart.
 */
export function KvCache() {
  const queryClient = useQueryClient();
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const system = useQuery({ queryKey: ["systemInfo"], queryFn: api.systemInfo });
  const current = llmPrefs.data?.kv_cache_type ?? "";

  // Hardware-aware suggestion (#329): tight VRAM → q8_0/q4_0, ample → f16.
  const rec = recommendKvCache(system.data);

  const save = useMutation({
    mutationFn: (value: string | null) => api.setKvCacheType(value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llmPrefs"] }),
  });

  return (
    <Card>
      <h3 className="mb-1 font-serif text-base text-ink">KV-cache type</h3>
      <p className="mb-3 text-xs leading-relaxed text-ink-dim">
        Quantize the attention cache to fit a longer context in less VRAM (a small quality
        trade-off). Applies to all local models; flash attention is enabled automatically.
      </p>
      {llmPrefs.isLoading ? (
        <Spinner />
      ) : (
        <label className="block">
          <span className="sr-only">KV-cache type</span>
          <Select
            className="w-full"
            value={current}
            disabled={save.isPending}
            onChange={(e) => save.mutate(e.target.value || null)}
          >
            {KV_CACHE_OPTIONS.map((opt) => (
              <option key={opt.value || "default"} value={opt.value}>
                {opt.label}
                {rec && rec.value === opt.value ? " · suggested" : ""}
              </option>
            ))}
          </Select>
        </label>
      )}

      {/* hardware-aware suggestion — mirrors the context-window "Suggested" hint (#329) */}
      {rec && rec.value !== current && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-(--radius-field) border border-accent/30 bg-accent-dim/40 px-3 py-2 text-sm">
          <Sparkles size={14} className="shrink-0 text-accent" />
          <span className="text-ink">
            Suggested <strong>{rec.name}</strong>
          </span>
          <Button
            variant="outline"
            className="ml-auto"
            onClick={() => save.mutate(rec.value || null)}
            disabled={save.isPending}
          >
            Use {rec.name}
          </Button>
        </div>
      )}
      {rec && (
        <p className="mt-2 text-[11px] italic leading-relaxed text-ink-faint">
          {rec.value === current ? "Recommended for your hardware — " : ""}
          {rec.reason}
        </p>
      )}

      {save.isPending ? (
        <p className="mt-2 text-[11px] text-ink-faint">Applying — restarting Ollama…</p>
      ) : save.isSuccess && save.data.applied ? (
        <p className="mt-2 text-[11px] leading-relaxed text-ink-dim">
          Applied — Ollama restarted with the new cache type (a few seconds to warm back up).
        </p>
      ) : save.isSuccess && !save.data.applied ? (
        <p className="mt-2 text-[11px] leading-relaxed text-warn">
          Saved, but the core couldn’t restart Ollama (no Docker access). Set{" "}
          <code className="font-mono">OLLAMA_KV_CACHE_TYPE</code>
          {current ? ` (${current})` : ""} and{" "}
          <code className="font-mono">OLLAMA_FLASH_ATTENTION=1</code> in your environment, then
          restart Ollama.
        </p>
      ) : null}
      {save.isError && <p className="mt-1 text-sm text-danger">{(save.error as Error).message}</p>}
    </Card>
  );
}

// ── Embedding default ─────────────────────────────────────────────────────────

export function EmbedDefault() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const [settingsOpen, setSettingsOpen] = useState(false);

  const current = llmPrefs.data?.global_embed_default ?? "";
  const available = (models.data ?? []).filter((m) => !m.hidden);

  const setEmbedDefault = useMutation({
    mutationFn: (model: string | null) => api.setGlobalEmbedDefault(model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llmPrefs"] }),
  });

  // Re-embed everything (#332): rebuild every reindexable module's vectors with the current
  // embedding model. Changing the model above doesn't re-embed existing data on its own.
  const reembed = useMutation({ mutationFn: () => api.reembed() });

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
        <div className="flex items-center gap-2">
          <label className="block flex-1">
            <span className="sr-only">Global embedding model</span>
            <Select
              className="w-full"
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
            </Select>
          </label>
          {current && (
            <Button
              variant="ghost"
              aria-label={`Settings for ${current}`}
              onClick={() => setSettingsOpen(true)}
            >
              <SlidersHorizontal size={14} />
              Settings
            </Button>
          )}
        </div>
      )}
      {setEmbedDefault.isError && (
        <p className="mt-2 text-sm text-danger">{(setEmbedDefault.error as Error).message}</p>
      )}

      {/* re-embed everything (#332) */}
      <div className="mt-4 border-t border-edge pt-3">
        <p className="mb-2 text-xs leading-relaxed text-ink-dim">
          Changing the embedding model doesn't re-embed existing data on its own — vectors built
          with the old model won't match new queries. Re-embed to rebuild every module's index
          with the current model. It runs in the background and can take a while.
        </p>
        <Button variant="outline" busy={reembed.isPending} onClick={() => reembed.mutate()}>
          <RefreshCw size={14} />
          Re-embed everything
        </Button>
        {reembed.isSuccess &&
          (reembed.data.modules.length === 0 ? (
            <p className="mt-2 text-[11px] text-ink-dim">
              No embedding-backed modules to re-embed.
            </p>
          ) : (
            <div className="mt-2 text-[11px] text-ink-dim">
              Re-embedding started — rebuilding in the background:
              <ul className="mt-1 flex flex-col gap-0.5">
                {reembed.data.modules.map((m) => (
                  <li key={m.module} className="flex items-center gap-1.5">
                    <Dot tone={m.status === "started" ? "accent" : "danger"} />
                    <span className="font-mono">{m.module}</span>
                    <span>· {m.status === "started" ? "started" : "failed to start"}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        {reembed.isError && (
          <p className="mt-2 text-sm text-danger">{(reembed.error as Error).message}</p>
        )}
      </div>

      <ModelSettingsSheet
        model={settingsOpen && current ? current : null}
        onClose={() => setSettingsOpen(false)}
      />
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

// ── Saved hosted models (#496) ──────────────────────────────────────────────────

/**
 * Saved hosted models — the hosted/API model ids the operator has used, now first-class
 * per-tenant rows (#496): server-persisted so they survive a PWA reinstall and follow the
 * account across devices. Grouped under their provider; each can be set as the global default
 * (the star that local rows already have) or removed. New ids are added from the chat picker.
 */
export function SavedHostedModels() {
  const queryClient = useQueryClient();
  const saved = useQuery({ queryKey: ["savedModels"], queryFn: () => api.savedModels() });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const globalDefault = llmPrefs.data?.global_default ?? null;
  // The hosted id whose settings sheet is open (a context budget, #570), or null when closed.
  const [settingsFor, setSettingsFor] = useState<string | null>(null);

  const setDefault = useMutation({
    mutationFn: (model: string | null) => api.setGlobalDefault(model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llmPrefs"] }),
  });
  const remove = useMutation({
    mutationFn: (model: string) => api.removeSavedModel(model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["savedModels"] }),
  });

  // Group by provider alias, preserving the server's most-recent-first order within each group.
  const groups = new Map<string, SavedHostedModel[]>();
  for (const m of saved.data ?? []) {
    groups.set(m.provider, [...(groups.get(m.provider) ?? []), m]);
  }

  return (
    <Card>
      <h3 className="mb-1 font-serif text-base text-ink">Hosted models</h3>
      <p className="mb-3 text-xs leading-relaxed text-ink-dim">
        The hosted model ids you've used, saved to your account so they follow you across
        devices. Star one to make it the global default, or remove it — add new ids from the
        model picker in a chat.
      </p>
      {saved.isLoading ? (
        <Spinner />
      ) : (saved.data ?? []).length === 0 ? (
        <p className="text-sm text-ink-dim">
          None yet — pick a hosted model in a chat (e.g.{" "}
          <code className="font-mono">claude/…</code>) and it's saved here.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {[...groups.entries()].map(([provider, models]) => (
            <div key={provider}>
              <p className="mb-1 text-[11px] uppercase tracking-wide text-ink-faint">
                {PROVIDER_LABELS[provider] ?? provider}
              </p>
              <div className="flex flex-col gap-1">
                {models.map((m) => {
                  const id = m.model;
                  const isDefault = globalDefault === id;
                  return (
                    <div
                      key={id}
                      className="flex items-center gap-2 rounded-(--radius-field) border border-edge px-3 py-2"
                    >
                      <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">
                        {id}
                      </span>
                      {isDefault && <Badge tone="accent">default</Badge>}
                      {m.context_length != null && (
                        <Tooltip label={`${m.context_length.toLocaleString()} token context`}>
                          <Badge tone="dim">{formatContextLength(m.context_length)}</Badge>
                        </Tooltip>
                      )}
                      <Tooltip label="Context budget">
                        <Button
                          variant="ghost"
                          aria-label={`Settings for ${id}`}
                          onClick={() => setSettingsFor(id)}
                        >
                          <SlidersHorizontal size={14} />
                        </Button>
                      </Tooltip>
                      <Tooltip label={isDefault ? "Default model" : "Set as default"}>
                        <Button
                          variant="ghost"
                          aria-label={isDefault ? `${id} is the default` : `Set ${id} as default`}
                          onClick={() => setDefault.mutate(isDefault ? null : id)}
                          disabled={setDefault.isPending}
                        >
                          <Star size={14} fill={isDefault ? "currentColor" : "none"} />
                        </Button>
                      </Tooltip>
                      <Button
                        variant="ghost"
                        aria-label={`Remove ${id}`}
                        onClick={() => remove.mutate(id)}
                        disabled={remove.isPending}
                      >
                        <Trash2 size={14} />
                      </Button>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
      {(setDefault.isError || remove.isError) && (
        <p className="mt-2 text-sm text-danger">
          {((setDefault.error ?? remove.error) as Error)?.message}
        </p>
      )}
      <HostedModelSettingsSheet model={settingsFor} onClose={() => setSettingsFor(null)} />
    </Card>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

export function ModelsScreen() {
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const installed = new Set((models.data ?? []).map((m) => m.name));

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <h1 className="font-serif text-xl text-ink">Models</h1>
        <CatalogBrowser installed={installed} />
        <DownloadTray />
        <LocalModels />
        <ContextWindow />
        <KvCache />
        <EmbedDefault />
        <Providers />
        <SavedHostedModels />
      </div>
    </div>
  );
}
