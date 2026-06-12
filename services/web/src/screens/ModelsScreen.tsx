/**
 * Models — the model manager. Local models (pull with live progress, delete,
 * loaded state) and hosted providers (key entry → core → OpenBao; the key
 * never comes back).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, KeyRound, Trash2 } from "lucide-react";
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
import { api } from "@/lib/api";
import { PROVIDER_LABELS, PROVIDER_MODEL_HINTS, formatBytes } from "@/lib/format";
import type { ProviderInfo } from "@/lib/contracts";
import { useDownloads } from "@/stores/downloads";
import { usePrefs } from "@/stores/prefs";

function PullBox() {
  const queryClient = useQueryClient();
  const pull = useDownloads((s) => s.pull);
  const active = useDownloads((s) => s.active);
  const dismiss = useDownloads((s) => s.dismiss);
  const [name, setName] = useState("");

  const start = (model: string) => {
    if (!model.trim()) return;
    setName("");
    void pull(model.trim(), () => queryClient.invalidateQueries({ queryKey: ["models"] }));
  };

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Pull a model</h3>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          start(name);
        }}
      >
        <TextInput
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. llama3.2 or qwen2.5:0.5b"
          aria-label="Model to pull"
        />
        <Button type="submit" variant="primary" disabled={!name.trim()}>
          <Download size={15} />
          Pull
        </Button>
      </form>
      <div className="mt-3 flex flex-col gap-2">
        {Object.values(active).map((download) => {
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
                    className={cn("h-full rounded-full bg-accent transition-all", pct == null && "ep-breathe w-1/3")}
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

function LocalModels() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const prefModel = usePrefs((s) => s.model);
  const setModel = usePrefs((s) => s.setModel);
  const [confirming, setConfirming] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: api.deleteModel,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }),
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
            className="group flex items-center gap-3 rounded-(--radius-field) px-2 py-2 hover:bg-surface-2"
          >
            <button
              className="flex min-w-0 flex-1 items-center gap-2 text-left"
              onClick={() => setModel(model.name)}
              title="Use for new chats"
            >
              <span className="truncate text-sm text-ink">{model.name}</span>
              {model.loaded && <Badge tone="ok">loaded</Badge>}
              {prefModel === model.name && <Badge tone="accent">chatting</Badge>}
            </button>
            <span className="text-xs text-ink-faint">{formatBytes(model.size)}</span>
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

export function ModelsScreen() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <h1 className="font-serif text-xl text-ink">Models</h1>
        <PullBox />
        <LocalModels />
        <Providers />
      </div>
    </div>
  );
}
