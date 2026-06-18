/**
 * The `editor` archetype (ADR-0018): an Obsidian-like document editor, core-rendered
 * and **shared** — knowledge is the first user (#130), notes the second (#134). The
 * module supplies only data through the core proxy: a document/folder tree
 * (`GET /pages/{id}`), one document's content (`GET …/doc`), and a save (`PUT …/doc`).
 * No module markup runs here.
 *
 * Authoring (ADR-0026): when the page's data sets `can_create`, the editor shows a
 * "New note" control that opens a blank buffer at a fresh slug; the first save creates
 * the document (knowledge leaves `can_create` false — its notes are authored in
 * Obsidian). The shell derives the slug; the module derives the title from the body.
 *
 * File tree management (#216): when the page's data sets `can_manage_files`, the shell
 * shows folder CRUD controls — create/delete folders, delete files, and rename files.
 * Knowledge sets this flag; notes does not.
 *
 * Layout mirrors the browser archetype: list + detail side-by-side on wide screens; on
 * phones the list fills the view and opening a document slides to the editor with a
 * back affordance. Edit shows the markdown source; Preview renders it with the shell's
 * prose styler. Saving persists through the core, which (for knowledge/notes) re-indexes.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  FileText,
  Folder,
  FolderOpen,
  MoreHorizontal,
  Plus,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { Markdown } from "@/components/Markdown";
import { Badge, Button, EmptyState, Spinner, TextArea, TextInput, cn } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { api } from "@/lib/api";
import { EditorData } from "@/lib/contracts";
import type { EditorDoc } from "@/lib/contracts";

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

// ── Tree builder ───────────────────────────────────────────────────────────────

interface TreeNode {
  path: string;
  title: string;
  type: "file" | "dir";
  children: TreeNode[];
  depth: number;
}

/**
 * Build a nested tree from the flat list returned by the module. Folders come
 * before files at each level; within each type nodes are sorted alphabetically.
 */
function buildTree(docs: EditorDoc[]): TreeNode[] {
  const root: TreeNode[] = [];
  // Map from dir-path → its children array in the tree, for O(1) lookup.
  const dirMap = new Map<string, TreeNode[]>();

  for (const doc of docs) {
    const parts = doc.path.split("/");
    const depth = parts.length - 1;
    const parentPath = parts.slice(0, -1).join("/");
    const siblings = parentPath ? (dirMap.get(parentPath) ?? root) : root;

    const node: TreeNode = {
      path: doc.path,
      title: doc.title,
      type: doc.type,
      children: [],
      depth,
    };
    siblings.push(node);
    if (doc.type === "dir") {
      dirMap.set(doc.path, node.children);
    }
  }

  // Sort each level: dirs before files, then alphabetically within each group.
  function sortLevel(nodes: TreeNode[]): void {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
      return a.title.localeCompare(b.title);
    });
    for (const node of nodes) sortLevel(node.children);
  }
  sortLevel(root);
  return root;
}

// ── TreeItem component ─────────────────────────────────────────────────────────

interface TreeItemProps {
  node: TreeNode;
  selectedPath: string | null;
  collapsed: Set<string>;
  hoveredPath: string | null;
  renamingPath: string | null;
  renameValue: string;
  module: string;
  pageId: string;
  canManageFiles: boolean;
  onSelect: (path: string) => void;
  onToggleCollapse: (path: string) => void;
  onSetHovered: (path: string | null) => void;
  onStartNewFileInFolder: (folderPath: string) => void;
  onDeleteFile: (path: string) => void;
  onDeleteFolder: (path: string) => void;
  onStartRename: (path: string, currentTitle: string) => void;
  onRenameChange: (value: string) => void;
  onRenameSubmit: (oldPath: string) => void;
  onRenameDismiss: () => void;
}

function TreeItem({
  node,
  selectedPath,
  collapsed,
  hoveredPath,
  renamingPath,
  renameValue,
  module,
  pageId,
  canManageFiles,
  onSelect,
  onToggleCollapse,
  onSetHovered,
  onStartNewFileInFolder,
  onDeleteFile,
  onDeleteFolder,
  onStartRename,
  onRenameChange,
  onRenameSubmit,
  onRenameDismiss,
}: TreeItemProps) {
  const isDir = node.type === "dir";
  const isCollapsed = collapsed.has(node.path);
  const isSelected = node.path === selectedPath;
  const isHovered = hoveredPath === node.path;
  const isRenaming = renamingPath === node.path;
  const indent = node.depth * 12;

  return (
    <>
      <li>
        <div
          className={cn(
            "group flex w-full items-center gap-1.5 rounded-(--radius-field) px-2 py-1.5 text-left transition-colors",
            isSelected && !isDir
              ? "bg-accent-dim text-accent-strong"
              : "text-ink hover:bg-surface-2",
          )}
          style={{ paddingLeft: `${8 + indent}px` }}
          onMouseEnter={() => onSetHovered(node.path)}
          onMouseLeave={() => onSetHovered(null)}
        >
          {/* collapse toggle for dirs */}
          {isDir && (
            <button
              onClick={() => onToggleCollapse(node.path)}
              className="shrink-0 text-ink-faint hover:text-ink"
              aria-label={isCollapsed ? "Expand folder" : "Collapse folder"}
            >
              {isCollapsed ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            </button>
          )}

          {/* icon */}
          {isDir ? (
            isCollapsed ? (
              <Folder size={14} className="shrink-0 text-ink-faint" />
            ) : (
              <FolderOpen size={14} className="shrink-0 text-ink-faint" />
            )
          ) : (
            <FileText size={14} className="shrink-0 text-ink-faint" />
          )}

          {/* name / rename input */}
          {isRenaming && !isDir ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                onRenameSubmit(node.path);
              }}
              className="flex flex-1 items-center gap-1"
            >
              <TextInput
                autoFocus
                value={renameValue}
                onChange={(e) => onRenameChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") onRenameDismiss();
                }}
                className="h-6 py-0 text-xs"
                aria-label="Rename file"
              />
              <Button type="submit" variant="primary" className="h-6 px-2 py-0 text-xs">
                OK
              </Button>
            </form>
          ) : (
            <button
              onClick={() => {
                if (isDir) {
                  onToggleCollapse(node.path);
                } else {
                  onSelect(node.path);
                }
              }}
              className="min-w-0 flex-1 text-left"
            >
              <span className="block truncate text-sm">{node.title}</span>
            </button>
          )}

          {/* per-item actions (shown on hover when can_manage_files) */}
          {canManageFiles && isHovered && !isRenaming && (
            <div className="ml-auto flex shrink-0 items-center gap-0.5">
              {isDir && (
                <button
                  title="New file in folder"
                  onClick={() => onStartNewFileInFolder(node.path)}
                  className="rounded p-0.5 text-ink-faint hover:text-ink hover:bg-surface-3"
                >
                  <Plus size={12} />
                </button>
              )}
              <button
                title={isDir ? "Delete folder" : "Delete file"}
                onClick={() => {
                  if (isDir) {
                    onDeleteFolder(node.path);
                  } else {
                    onDeleteFile(node.path);
                  }
                }}
                className="rounded p-0.5 text-ink-faint hover:text-danger hover:bg-surface-3"
              >
                <MoreHorizontal size={12} />
              </button>
              {!isDir && (
                <button
                  title="Rename file"
                  onClick={() => onStartRename(node.path, node.title)}
                  className="rounded p-0.5 text-ink-faint hover:text-ink hover:bg-surface-3"
                >
                  <ChevronRight size={12} />
                </button>
              )}
            </div>
          )}

          {/* mobile chevron for files */}
          {!isDir && !canManageFiles && (
            <ChevronRight size={15} className="ml-auto shrink-0 text-ink-faint sm:hidden" />
          )}
        </div>
      </li>

      {/* children (rendered when not collapsed) */}
      {isDir && !isCollapsed && node.children.length > 0 && (
        <>
          {node.children.map((child) => (
            <TreeItem
              key={child.path}
              node={child}
              selectedPath={selectedPath}
              collapsed={collapsed}
              hoveredPath={hoveredPath}
              renamingPath={renamingPath}
              renameValue={renameValue}
              module={module}
              pageId={pageId}
              canManageFiles={canManageFiles}
              onSelect={onSelect}
              onToggleCollapse={onToggleCollapse}
              onSetHovered={onSetHovered}
              onStartNewFileInFolder={onStartNewFileInFolder}
              onDeleteFile={onDeleteFile}
              onDeleteFolder={onDeleteFolder}
              onStartRename={onStartRename}
              onRenameChange={onRenameChange}
              onRenameSubmit={onRenameSubmit}
              onRenameDismiss={onRenameDismiss}
            />
          ))}
        </>
      )}
    </>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

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

  // File tree state
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [hoveredPath, setHoveredPath] = useState<string | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [newFolderCreating, setNewFolderCreating] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  // The folder inside which a new file is being created (null = root)
  const [newFileInFolder, setNewFileInFolder] = useState<string | null>(null);

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
      setIsNew(false);
      void qc.invalidateQueries({ queryKey: ["module-page", module, pageId] });
    },
  });

  const dirty = draft !== baseline;

  // ── Folder / file tree mutations ──────────────────────────────────────────

  const invalidateList = () =>
    void qc.invalidateQueries({ queryKey: ["module-page", module, pageId] });

  const createFolder = useMutation({
    mutationFn: (path: string) => api.createModuleFolder(module, pageId, path),
    onSuccess: () => {
      setNewFolderCreating(false);
      setNewFolderName("");
      invalidateList();
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      window.alert(`Could not create folder: ${msg}`);
    },
  });

  const deleteDoc = useMutation({
    mutationFn: (path: string) => api.deleteModuleDoc(module, pageId, path),
    onSuccess: (_, path) => {
      if (selectedPath === path) {
        setSelectedPath(null);
        setIsNew(false);
      }
      invalidateList();
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      window.alert(`Could not delete file: ${msg}`);
    },
  });

  const deleteFolder = useMutation({
    mutationFn: (path: string) => api.deleteModuleFolder(module, pageId, path),
    onSuccess: invalidateList,
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      window.alert(`Could not delete folder: ${msg}`);
    },
  });

  const moveItem = useMutation({
    mutationFn: ({ from, to }: { from: string; to: string }) =>
      api.moveModuleItem(module, pageId, from, to),
    onSuccess: (result, { from }) => {
      if (selectedPath === from) setSelectedPath(result.path);
      setRenamingPath(null);
      setRenameValue("");
      invalidateList();
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      window.alert(`Could not rename: ${msg}`);
    },
  });

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
  const tree = buildTree(data.docs);

  const openDoc = (path: string) => {
    setIsNew(false);
    setSelectedPath(path);
  };

  // can_create: the existing "New note" flow (used by Notes module)
  const startNewNote = (event: FormEvent) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    const folderPrefix = newFileInFolder ? `${newFileInFolder}/` : "";
    const taken = new Set(data.docs.map((d) => d.path));
    const slug = uniqueSlug(folderPrefix + slugify(name), taken);
    setIsNew(true);
    setSelectedPath(slug);
    setDraft(`# ${name}\n\n`);
    setBaseline("");
    setMode("edit");
    setCreating(false);
    setNewName("");
    setNewFileInFolder(null);
  };

  // can_manage_files: start a new file inside a folder
  const handleStartNewFileInFolder = (folderPath: string) => {
    setNewFileInFolder(folderPath);
    const taken = new Set(data.docs.map((d) => d.path));
    const slug = uniqueSlug(`${folderPath}/new-note.md`, taken);
    setIsNew(true);
    setSelectedPath(slug);
    setDraft("");
    setBaseline("");
    setMode("edit");
  };

  const handleDeleteFile = (path: string) => {
    if (!window.confirm(`Delete "${path}"? This cannot be undone.`)) return;
    deleteDoc.mutate(path);
  };

  const handleDeleteFolder = (path: string) => {
    if (!window.confirm(`Delete folder "${path}"? It must be empty.`)) return;
    deleteFolder.mutate(path);
  };

  const handleStartRename = (path: string, currentTitle: string) => {
    setRenamingPath(path);
    setRenameValue(currentTitle);
  };

  const handleRenameSubmit = (oldPath: string) => {
    const name = renameValue.trim();
    if (!name) return;
    const parts = oldPath.split("/");
    parts[parts.length - 1] = name.endsWith(".md") ? name : `${slugify(name)}.md`;
    const newPath = parts.join("/");
    if (newPath === oldPath) {
      setRenamingPath(null);
      return;
    }
    moveItem.mutate({ from: oldPath, to: newPath });
  };

  const handleCreateFolder = (event: FormEvent) => {
    event.preventDefault();
    const name = newFolderName.trim();
    if (!name) return;
    createFolder.mutate(slugify(name));
  };

  const toggleCollapse = (path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const commonTreeProps = {
    selectedPath,
    collapsed,
    hoveredPath,
    renamingPath,
    renameValue,
    module,
    pageId,
    canManageFiles: data.can_manage_files,
    onSelect: openDoc,
    onToggleCollapse: toggleCollapse,
    onSetHovered: setHoveredPath,
    onStartNewFileInFolder: handleStartNewFileInFolder,
    onDeleteFile: handleDeleteFile,
    onDeleteFolder: handleDeleteFolder,
    onStartRename: handleStartRename,
    onRenameChange: setRenameValue,
    onRenameSubmit: handleRenameSubmit,
    onRenameDismiss: () => {
      setRenamingPath(null);
      setRenameValue("");
    },
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
        {/* can_create toolbar (Notes module) */}
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

        {/* can_manage_files toolbar (Knowledge module) */}
        {data.can_manage_files && (
          <div className="border-b border-edge p-2">
            {newFolderCreating ? (
              <form onSubmit={handleCreateFolder} className="flex items-center gap-2">
                <TextInput
                  autoFocus
                  value={newFolderName}
                  onChange={(e) => setNewFolderName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      setNewFolderCreating(false);
                      setNewFolderName("");
                    }
                  }}
                  placeholder="Folder name…"
                  aria-label="New folder name"
                />
                <Button
                  type="submit"
                  variant="primary"
                  disabled={!newFolderName.trim()}
                  busy={createFolder.isPending}
                >
                  Create
                </Button>
              </form>
            ) : (
              <Button
                variant="ghost"
                className="w-full justify-start"
                onClick={() => setNewFolderCreating(true)}
              >
                <Folder size={15} /> New folder
              </Button>
            )}
          </div>
        )}

        {data.docs.filter((d) => d.type === "file").length === 0 &&
        data.docs.filter((d) => d.type === "dir").length === 0 ? (
          data.can_manage_files ? (
            <EmptyState quote="No documents yet — create a folder or add files." />
          ) : data.can_create ? (
            <EmptyState quote="No notes yet — create one with New note." />
          ) : (
            <EmptyState quote="An empty vault. Add notes in Obsidian and they appear here." />
          )
        ) : (
          <ul className="flex flex-col p-2">
            {tree.map((node) => (
              <TreeItem key={node.path} node={node} {...commonTreeProps} />
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
        ) : doc.isLoading && !isNew ? (
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
