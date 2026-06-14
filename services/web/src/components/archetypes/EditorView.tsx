/**
 * The `editor` archetype (ADR-0018): an Obsidian-like document editor, core-rendered
 * and **shared** — the knowledge vault page is the first user (#130); Notes reuses it
 * verbatim. The module supplies only data through the core proxy: a document list
 * (`GET /pages/{id}`), one document's content (`GET …/doc`), and a save (`PUT …/doc`).
 * No module markup runs here.
 *
 * Layout mirrors the browser archetype: list + detail side-by-side on wide screens; on
 * phones the list fills the view and opening a document slides to the editor with a
 * back affordance. Edit shows the markdown source; Preview renders it with the shell's
 * prose styler. Saving persists through the core, which (for knowledge) re-indexes.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Markdown } from "@/components/Markdown";
import { Badge, Button, EmptyState, Spinner, TextArea, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { EditorData } from "@/lib/contracts";

export function EditorView({ module, pageId }: { module: string; pageId: string }) {
  const qc = useQueryClient();
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const [draft, setDraft] = useState("");
  const [baseline, setBaseline] = useState("");

  // Deep-link: open the document named by `?doc=` (e.g. a knowledge hover-card's
  // "Open in Knowledge" link, #143). Re-applies if the param changes while mounted.
  const [searchParams] = useSearchParams();
  const docParam = searchParams.get("doc");
  useEffect(() => {
    if (docParam) setSelectedPath(docParam);
  }, [docParam]);

  const list = useQuery({
    queryKey: ["module-page", module, pageId],
    queryFn: () => api.modulePage(module, pageId),
  });

  const doc = useQuery({
    queryKey: ["module-doc", module, pageId, selectedPath],
    queryFn: () => api.modulePageDoc(module, pageId, selectedPath as string),
    enabled: selectedPath != null,
  });

  // Seed the editor buffer when a document loads; track the saved baseline so we
  // know when there are unsaved changes.
  useEffect(() => {
    if (doc.data) {
      setDraft(doc.data.content);
      setBaseline(doc.data.content);
      setMode("edit");
    }
  }, [doc.data]);

  const save = useMutation({
    mutationFn: () => api.saveModulePageDoc(module, pageId, selectedPath as string, draft),
    onSuccess: () => {
      setBaseline(draft);
      // A newly created document joins the list; refresh it.
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

  return (
    <div className="grid h-full min-h-0 sm:grid-cols-[minmax(0,18rem)_1fr]">
      {/* document list — hidden on phone once a document is open */}
      <div
        className={cn(
          "min-h-0 overflow-y-auto border-edge sm:border-r",
          selectedPath && "hidden sm:block",
        )}
      >
        {data.docs.length === 0 ? (
          <EmptyState quote="An empty vault. Add notes in Obsidian and they appear here." />
        ) : (
          <ul className="flex flex-col p-2">
            {data.docs.map((d) => (
              <li key={d.id}>
                <button
                  onClick={() => setSelectedPath(d.path)}
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
                onClick={() => setSelectedPath(null)}
                className="inline-flex items-center gap-1 text-sm text-ink-dim hover:text-ink sm:hidden"
              >
                <ChevronLeft size={15} /> back
              </button>
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-dim">
                {doc.data?.path}
              </span>
              {save.data && !save.data.indexed && (
                <Badge tone="warn">saved · not indexed</Badge>
              )}
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
                  aria-label={`Edit ${doc.data?.path}`}
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
