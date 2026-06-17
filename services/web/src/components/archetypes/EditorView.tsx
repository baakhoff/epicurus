/**
 * The `editor` archetype (ADR-0018): an Obsidian-like document editor, core-rendered
 * and **shared** — knowledge is the first user (#130), notes the second (#134). The
 * module supplies only data through the core proxy: a document list (`GET /pages/{id}`),
 * one document's content (`GET …/doc`), and a save (`PUT …/doc`). No module markup runs
 * here.
 *
 * Authoring (ADR-0026): when the page's data sets `can_create`, the editor shows a
 * "New note" control that opens a blank buffer at a fresh slug; the first save creates
 * the document (knowledge leaves `can_create` false — its notes are authored in
 * Obsidian). The shell derives the slug; the module derives the title from the body.
 *
 * Layout mirrors the browser archetype: list + detail side-by-side on wide screens; on
 * phones the list fills the view and opening a document slides to the editor with a
 * back affordance. Edit shows the markdown source; Preview renders it with the shell's
 * prose styler. Saving persists through the core, which (for knowledge/notes) re-indexes.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, FileText, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { Markdown } from "@/components/Markdown";
import { Badge, Button, EmptyState, Spinner, TextArea, TextInput, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { EditorData } from "@/lib/contracts";

/** A filesystem-safe, readable slug for a note title (the editor `path`). */
function slugify(name: string): string {
  const base = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return base || "note";
}

/** Disambiguate a slug against the ones already in the list (append -2, -3, …). */
function uniqueSlug(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

export function EditorView({ module, pageId }: { module: string; pageId: string }) {
  const qc = useQueryClient();
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  // True while the selected slug is a not-yet-saved new note: skip the doc fetch
  // (a GET would 404) and keep the seeded buffer.
  const [isNew, setIsNew] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const [draft, setDraft] = useState("");
  const [baseline, setBaseline] = useState("");

  // Deep-link: open the document named by `?doc=` (e.g. a knowledge hover-card's
  // "Open in Knowledge" link, #143). Re-applies if the param changes while mounted.
  const [searchParams] = useSearchParams();
  const docParam = searchParams.get("doc");
  // Apply the `?doc=` deep-link when it changes — adjust state during render
  // (the React-blessed alternative to a setState-in-effect).
  const [appliedDoc, setAppliedDoc] = useState<string | null>(null);
  if (docParam && docParam !== appliedDoc) {
    setAppliedDoc(docParam);
    setSelectedPath(docParam);
  }

  const list = useQuery({
    queryKey: ["module-page", module, pageId],
    queryFn: () => api.modulePage(module, pageId),
  });

  const doc = useQuery({
    queryKey: ["module-doc", module, pageId, selectedPath],
    queryFn: () => api.modulePageDoc(module, pageId, selectedPath as string),
    enabled: selectedPath != null && !isNew,
  });

  // Seed the editor buffer when a document loads; track the saved baseline so we
  // know when there are unsaved changes. Adjust state during render keyed on the
  // loaded doc's identity (the React-blessed alternative to a setState-in-effect).
  const [seededDoc, setSeededDoc] = useState<typeof doc.data>(undefined);
  if (doc.data && doc.data !== seededDoc) {
    setSeededDoc(doc.data);
    setDraft(doc.data.content);
    setBaseline(doc.data.content);
    setMode("edit");
  }

  const save = useMutation({
    mutationFn: () => api.saveModulePageDoc(module, pageId, selectedPath as string, draft),
    onSuccess: () => {
      setBaseline(draft);
      // The note now exists — let the doc fetch take over on re-select…
      setIsNew(false);
      // …and the newly created note joins the list; refresh it.
      void qc.invalidateQueries({ queryKey: ["module-page", module, pageId] });
    },
  });

  const dirty = draft !== baseline;

  if (list.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (list.isError) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState quote="This page is resting.">
          <p className="text-sm text-ink-dim">{(list.error as Error).message}</p>
        </EmptyState>
      </div>
    );
  }

  const data = EditorData.parse(list.data ?? {});

  const openDoc = (path: string) => {
    setIsNew(false);
    setSelectedPath(path);
  };

  const startNewNote = (event: FormEvent) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    const taken = new Set(data.docs.map((d) => d.path));
    const slug = uniqueSlug(slugify(name), taken);
    setIsNew(true);
    setSelectedPath(slug);
    setDraft(`# ${name}\n\n`); // module derives the title from this first heading
    setBaseline(""); // differs from the seed → Save is enabled immediately
    setMode("edit");
    setCreating(false);
    setNewName("");
  };

  return (
    <div className="grid h-full min-h-0 sm:grid-cols-[minmax(0,18rem)_1fr]">
      {/* document list — hidden on phone once a document is open */}
      <div
        className={cn(
          "flex min-h-0 flex-col overflow-y-auto border-edge sm:border-r",
          selectedPath && "hidden sm:flex",
        )}
      >
        {data.can_create && (
          <div className="border-b border-edge p-2">
            {creating ? (
              <form onSubmit={startNewNote} className="flex items-center gap-2">
                <TextInput
                  autoFocus
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      setCreating(false);
                      setNewName("");
                    }
                  }}
                  placeholder="Note title…"
                  aria-label="New note title"
                />
                <Button type="submit" variant="primary" disabled={!newName.trim()}>
                  Create
                </Button>
              </form>
            ) : (
              <Button
                variant="ghost"
                className="w-full justify-start"
                onClick={() => setCreating(true)}
              >
                <Plus size={15} /> New note
              </Button>
            )}
          </div>
        )}
        {data.docs.length === 0 ? (
          data.can_create ? (
            <EmptyState quote="No notes yet — create one to begin." />
          ) : (
            <EmptyState quote="An empty vault. Add notes in Obsidian and they appear here." />
          )
        ) : (
          <ul className="flex flex-col p-2">
            {data.docs.map((d) => (
              <li key={d.id}>
                <button
                  onClick={() => openDoc(d.path)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-(--radius-field) px-3 py-2 text-left transition-colors",
                    d.path === selectedPath
                      ? "bg-accent-dim text-accent-strong"
                      : "text-ink hover:bg-surface-2",
                  )}
                >
                  <FileText size={15} className="shrink-0 text-ink-faint" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">{d.title}</span>
                    {d.path !== d.title && (
                      <span className="block truncate text-xs text-ink-faint">{d.path}</span>
                    )}
                  </span>
                  <ChevronRight size={15} className="shrink-0 text-ink-faint sm:hidden" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* editor — hidden on phone until a document is open */}
      <div className={cn("flex min-h-0 flex-col", !selectedPath && "hidden sm:flex")}>
        {!selectedPath ? (
          <div className="hidden h-full items-center justify-center sm:flex">
            <EmptyState quote="Select a document to read or edit it." />
          </div>
        ) : doc.isLoading ? (
          <div className="flex h-full items-center justify-center">
            <Spinner />
          </div>
        ) : doc.isError ? (
          <div className="flex h-full items-center justify-center p-6">
            <EmptyState quote="That document slipped away.">
              <p className="text-sm text-ink-dim">{(doc.error as Error).message}</p>
            </EmptyState>
          </div>
        ) : (
          <>
            {/* toolbar */}
            <div className="flex items-center gap-2 border-b border-edge px-3 py-2">
              <button
                onClick={() => {
                  setSelectedPath(null);
                  setIsNew(false);
                }}
                className="inline-flex items-center gap-1 text-sm text-ink-dim hover:text-ink sm:hidden"
              >
                <ChevronLeft size={15} /> back
              </button>
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-dim">
                {selectedPath}
                {isNew && <span className="ml-1 text-ink-faint">(new)</span>}
              </span>
              {save.data && !save.data.indexed && <Badge tone="warn">saved · not indexed</Badge>}
              {save.isSuccess && !dirty && save.data?.indexed && <Badge tone="ok">saved</Badge>}
              {dirty && <span className="text-xs text-ink-faint">unsaved</span>}
              <div className="inline-flex overflow-hidden rounded-(--radius-field) border border-edge">
                {(["edit", "preview"] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setMode(m)}
                    className={cn(
                      "px-2.5 py-1 text-xs transition-colors",
                      mode === m
                        ? "bg-accent-dim text-accent-strong"
                        : "text-ink-dim hover:bg-surface-2",
                    )}
                  >
                    {m === "edit" ? "Edit" : "Preview"}
                  </button>
                ))}
              </div>
              <Button
                variant="primary"
                onClick={() => save.mutate()}
                disabled={!dirty}
                busy={save.isPending}
              >
                Save
              </Button>
            </div>

            {/* body */}
            <div className="min-h-0 flex-1 overflow-y-auto">
              {mode === "edit" ? (
                <TextArea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
                      e.preventDefault();
                      if (dirty) save.mutate();
                    }
                  }}
                  spellCheck={false}
                  className="h-full min-h-full rounded-none border-0 bg-transparent font-mono text-[13px] leading-relaxed focus:border-0"
                  aria-label={`Edit ${selectedPath}`}
                />
              ) : (
                <div className="mx-auto max-w-2xl px-5 py-4">
                  <Markdown>{draft}</Markdown>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
