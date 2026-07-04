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
 * back affordance. Edit shows the raw markdown source; Preview is an editable rich-text
 * (WYSIWYG) surface (#377) that writes back to the same markdown buffer. Saving persists
 * through the core, which (for knowledge/notes) re-indexes.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  FileText,
  FilePlus,
  Folder,
  FolderOpen,
  History,
  Library,
  MoreHorizontal,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { Markdown } from "@/components/Markdown";
import {
  Badge,
  Button,
  Confirm,
  EmptyState,
  Spinner,
  TextArea,
  TextInput,
  cn,
} from "@/components/ui";
import { ApiError } from "@/lib/api";
import { api } from "@/lib/api";
import { EditorData } from "@/lib/contracts";
import type { EditorDoc, EditorScope, EditorVersionContent } from "@/lib/contracts";
import { relativeTime } from "@/lib/format";
import { toast } from "@/stores/toasts";

// The editable WYSIWYG Preview (#377) — lazy-loaded so Milkdown/ProseMirror never enters the
// main bundle (it loads only when a document is opened in the editor archetype).
const WysiwygEditor = lazy(() => import("./WysiwygEditor"));

/** Idle delay before an unsaved edit is flushed (ADR-0042). Saving re-embeds, so we do NOT
 *  save on every keystroke — only once the doc has been still this long (plus on leaving the
 *  page and on explicit Save). Long enough to mean "stopped editing", short enough to be safe. */
const IDLE_SAVE_MS = 4000;

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

// ── Scope (knowledge-base) switcher ────────────────────────────────────────────

/**
 * The project/knowledge-base switcher (#KB-refactor): picks the active scope and offers a
 * "New {noun}" control. Projects and the read-only reference scope (platform docs) are
 * grouped. Shown only when the page declares a `scope_noun` (knowledge does; notes doesn't).
 */
function ScopeSwitcher({
  scopes,
  active,
  noun,
  canCreate,
  canDelete,
  creating,
  onSelect,
  onStartCreate,
  onDelete,
}: {
  scopes: EditorScope[];
  active: string;
  noun: string;
  canCreate: boolean;
  canDelete: boolean;
  creating: boolean;
  onSelect: (id: string) => void;
  onStartCreate: () => void;
  onDelete: (scope: EditorScope) => void;
}) {
  const [open, setOpen] = useState(false);
  const projects = scopes.filter((s) => s.kind === "project");
  const refs = scopes.filter((s) => s.kind === "reference");
  const activeScope = scopes.find((s) => s.id === active);

  return (
    <div className="relative border-b border-edge p-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-2 rounded-(--radius-field) border border-edge px-2.5 py-1.5 text-sm text-ink transition-colors hover:bg-surface-2"
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <Library size={14} className="shrink-0 text-ink-faint" />
          <span className="truncate">{activeScope?.title ?? `Select a ${noun}`}</span>
        </span>
        <ChevronDown size={14} className="shrink-0 text-ink-faint" />
      </button>
      {open && (
        <>
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            className="fixed inset-0 z-10 cursor-default"
            onClick={() => setOpen(false)}
          />
          <div className="absolute inset-x-2 top-full z-20 mt-1 max-h-80 overflow-y-auto overscroll-contain rounded-(--radius-card) border border-edge bg-surface py-1 shadow-(--ep-shadow)">
            {projects.length > 0 && (
              <div className="px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-ink-faint">
                {noun}s
              </div>
            )}
            {projects.map((s) => (
              <div
                key={s.id}
                className={cn(
                  "flex items-center",
                  s.id === active ? "bg-accent-dim" : "hover:bg-surface-2",
                )}
              >
                <button
                  onClick={() => {
                    onSelect(s.id);
                    setOpen(false);
                  }}
                  className={cn(
                    "flex min-w-0 flex-1 items-center gap-2 px-3 py-1.5 text-left text-sm",
                    s.id === active ? "text-accent-strong" : "text-ink",
                  )}
                >
                  <Library size={13} className="shrink-0 text-ink-faint" />
                  <span className="truncate">{s.title}</span>
                </button>
                {canDelete && (
                  <button
                    type="button"
                    onClick={() => {
                      onDelete(s);
                      setOpen(false);
                    }}
                    aria-label={`Delete ${s.title}`}
                    title={`Delete this ${noun}`}
                    className="mr-1 shrink-0 rounded-(--radius-field) p-1 text-ink-faint transition-colors hover:bg-danger/10 hover:text-danger"
                  >
                    <Trash2 size={13} />
                  </button>
                )}
              </div>
            ))}
            {refs.length > 0 && (
              <div className="mt-1 border-t border-edge px-3 pb-1 pt-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-faint">
                Reference
              </div>
            )}
            {refs.map((s) => (
              <button
                key={s.id}
                onClick={() => {
                  onSelect(s.id);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm",
                  s.id === active ? "bg-accent-dim text-accent-strong" : "text-ink hover:bg-surface-2",
                )}
              >
                <FileText size={13} className="shrink-0 text-ink-faint" />
                <span className="truncate">{s.title}</span>
              </button>
            ))}
            {canCreate && (
              <button
                onClick={() => {
                  onStartCreate();
                  setOpen(false);
                }}
                disabled={creating}
                className="mt-1 flex w-full items-center gap-2 border-t border-edge px-3 py-1.5 text-left text-sm text-ink-dim hover:bg-surface-2 hover:text-ink"
              >
                <Plus size={13} className="shrink-0" /> New {noun}
              </button>
            )}
          </div>
        </>
      )}
    </div>
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
  // Client-side tree filter (#339): matches document titles/paths in the active scope.
  const [treeFilter, setTreeFilter] = useState("");

  // The active scope (knowledge base / project, #KB-refactor). Empty = let the module
  // default to the first project; the switcher and create-project flow drive it. Paths in
  // the tree are scope-relative; `toModulePath` prepends the scope at the API boundary.
  const [activeScope, setActiveScope] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  // The knowledge base awaiting a delete confirm (#340); null = no dialog open.
  const [scopeToDelete, setScopeToDelete] = useState<EditorScope | null>(null);
  // Destructive tree actions awaiting a themed confirm (#488) — window.confirm is banned.
  const [pathToDelete, setPathToDelete] = useState<string | null>(null);
  const [folderToDelete, setFolderToDelete] = useState<string | null>(null);
  const [confirmingRestore, setConfirmingRestore] = useState(false);

  // Version history (ADR-0046): the dropdown's open state and the past version being
  // previewed (read-only) in place of the live buffer. `null` = editing the current doc.
  const [historyOpen, setHistoryOpen] = useState(false);
  const [viewingVersion, setViewingVersion] = useState<EditorVersionContent | null>(null);

  const list = useQuery({
    queryKey: ["module-page", module, pageId, activeScope],
    queryFn: () => api.modulePage(module, pageId, activeScope ? { scope: activeScope } : undefined),
    // Keep the previous scope's tree on screen while the new one loads (no flicker).
    placeholderData: keepPreviousData,
  });

  const listData = list.data ? EditorData.parse(list.data) : null;
  // The resolved scope: the operator's pick, else the module's default (first project).
  const scope = activeScope || listData?.scope || "";
  // Tree paths are scope-relative; the module's doc/folder/move endpoints want the
  // knowledge-root-relative `<scope>/<path>`.
  const toModulePath = (p: string): string => (scope ? `${scope}/${p}` : p);

  // Deep-link: open the document named by `?doc=` (e.g. a knowledge hover-card's "Open in
  // Knowledge" link, #143). The link carries a root-relative `<project>/<path>`; split the
  // leading scope so we select the right knowledge base. Adjust state during render.
  const [searchParams] = useSearchParams();
  const docParam = searchParams.get("doc");
  const [appliedDoc, setAppliedDoc] = useState<string | null>(null);
  if (docParam && docParam !== appliedDoc) {
    setAppliedDoc(docParam);
    const slash = docParam.indexOf("/");
    if (slash > 0) {
      setActiveScope(docParam.slice(0, slash));
      setSelectedPath(docParam.slice(slash + 1));
    } else {
      setSelectedPath(docParam);
    }
    setIsNew(false);
  }

  const doc = useQuery({
    queryKey: ["module-doc", module, pageId, scope, selectedPath],
    queryFn: () => api.modulePageDoc(module, pageId, toModulePath(selectedPath as string)),
    enabled: selectedPath != null && !isNew,
  });

  // Seed the editor buffer when a document opens. Key the seed on the *path*, not the
  // query object: a background refetch (the list/doc re-reads after an auto-save) hands
  // back a fresh object that must NOT clobber keystrokes typed since. A document opens
  // in `preview` — it renders straight away (ADR-0042); the Edit toggle drops to source.
  // Adjust state during render (the React-blessed alternative to a setState-in-effect).
  const [seededPath, setSeededPath] = useState<string | null>(null);
  if (doc.data && selectedPath !== seededPath) {
    setSeededPath(selectedPath);
    setDraft(doc.data.content);
    setBaseline(doc.data.content);
    setMode("preview");
  }

  const save = useMutation({
    // The path travels with the save (not via a `selectedPath` closure) so a flush fired as
    // we navigate away still targets the doc the draft belongs to, even though the selection
    // has already moved on. The path is scope-relative; `toModulePath` prepends the active
    // knowledge base at the API boundary (#KB-refactor).
    mutationFn: ({ path, content }: { path: string; content: string }) =>
      api.saveModulePageDoc(module, pageId, toModulePath(path), content),
    onSuccess: (_result, { path, content }) => {
      void qc.invalidateQueries({ queryKey: ["module-page", module, pageId] });
      // Every save snapshots a version (ADR-0046) — refresh the open doc's history.
      void qc.invalidateQueries({ queryKey: ["module-doc-versions", module, pageId] });
      // Reconcile the buffer only if we're still on the doc we saved — a leave-flush of the
      // *previous* doc must not stamp its content onto the now-open one's baseline. Setting
      // the baseline to exactly what was persisted keeps edits made mid-save dirty.
      if (path === selectedPath) {
        setBaseline(content);
        setIsNew(false);
      }
    },
  });

  // Version history (ADR-0046): the save snapshots are fetched lazily when the History
  // dropdown opens; viewing one previews it read-only in place of the live buffer.
  const versioned = Boolean(list.data?.versioned);
  const versions = useQuery({
    queryKey: ["module-doc-versions", module, pageId, selectedPath],
    queryFn: () => api.modulePageDocVersions(module, pageId, selectedPath as string),
    enabled: historyOpen && versioned && selectedPath != null && !isNew,
  });
  const viewVersion = useMutation({
    mutationFn: (versionId: string) =>
      api.modulePageDocVersion(module, pageId, selectedPath as string, versionId),
    onSuccess: (v) => {
      setViewingVersion(v);
      setHistoryOpen(false);
    },
  });

  const dirty = draft !== baseline;
  // react-query's `mutate` is referentially stable; depending on it (not the whole `save`
  // object, which is a new reference every render) keeps timers/callbacks from churning.
  const { mutate: saveMutate } = save;

  // ── Save policy (ADR-0042) ─────────────────────────────────────────────────
  // Notes/knowledge re-embed on every save, so we deliberately do NOT save on each
  // keystroke. A save (and its re-index) happens only when the user (1) leaves the page,
  // (2) idles — leaves the document unchanged for IDLE_SAVE_MS — or (3) saves explicitly
  // (the Save button / Ctrl-S). A read-only (watched Obsidian) vault never saves.
  const readOnly = Boolean(list.data?.read_only);
  const savingRef = useRef(false);
  useEffect(() => {
    savingRef.current = save.isPending;
  }, [save.isPending]);

  // Latest values for the leave-the-page flushes below — a listener or unmount cleanup
  // captures a stale closure otherwise. Refreshed after every commit.
  const flushRef = useRef({ draft, baseline, selectedPath, seededPath, readOnly });
  useEffect(() => {
    flushRef.current = { draft, baseline, selectedPath, seededPath, readOnly };
  });
  // Persist the buffer iff it has unsaved changes for the path it belongs to. The
  // `selectedPath === seededPath` guard is the safety: between selecting a document and its
  // content loading, `draft` still holds the *previous* doc — without it a flush could write
  // that stale content onto the newly-selected path.
  const flush = useCallback(() => {
    const s = flushRef.current;
    if (
      s.selectedPath &&
      s.selectedPath === s.seededPath &&
      !s.readOnly &&
      s.draft !== s.baseline &&
      !savingRef.current
    ) {
      saveMutate({ path: s.selectedPath, content: s.draft });
    }
  }, [saveMutate]);

  // (2) Idle: flush once the document has gone untouched for IDLE_SAVE_MS.
  useEffect(() => {
    if (!selectedPath || selectedPath !== seededPath || readOnly || draft === baseline) return;
    const id = window.setTimeout(flush, IDLE_SAVE_MS);
    return () => window.clearTimeout(id);
  }, [draft, baseline, selectedPath, seededPath, readOnly, flush]);

  // (1) Leave the page: flush when the tab is hidden (closing / backgrounding — the reliable
  // "about to leave" hook) and on unmount (navigating away from the editor screen).
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === "hidden") flush();
    };
    document.addEventListener("visibilitychange", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      flush();
    };
  }, [flush]);

  // ── Folder / file tree mutations ──────────────────────────────────────────

  const invalidateList = () =>
    void qc.invalidateQueries({ queryKey: ["module-page", module, pageId] });

  const createFolder = useMutation({
    mutationFn: (path: string) => api.createModuleFolder(module, pageId, toModulePath(path)),
    onSuccess: () => {
      setNewFolderCreating(false);
      setNewFolderName("");
      invalidateList();
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      toast.error(`Could not create folder: ${msg}`);
    },
  });

  const createProject = useMutation({
    mutationFn: (name: string) => api.createModuleProject(module, pageId, name),
    onSuccess: (created) => {
      setCreatingProject(false);
      setNewProjectName("");
      // Switch into the new knowledge base.
      setActiveScope(created.id);
      setSelectedPath(null);
      setIsNew(false);
      invalidateList();
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      toast.error(`Could not create knowledge base: ${msg}`);
    },
  });

  const deleteProject = useMutation({
    mutationFn: (scopeId: string) => api.deleteModuleProject(module, pageId, scopeId),
    onSuccess: (_result, scopeId) => {
      setScopeToDelete(null);
      // If the deleted base was active, fall back to the module's default (first project).
      if (scopeId === scope) {
        setActiveScope("");
        setSelectedPath(null);
        setIsNew(false);
        setTreeFilter("");
      }
      invalidateList();
    },
    onError: (err) => {
      setScopeToDelete(null);
      const msg = err instanceof ApiError ? err.detail : String(err);
      toast.error(`Could not delete knowledge base: ${msg}`);
    },
  });

  const deleteDoc = useMutation({
    mutationFn: (path: string) => api.deleteModuleDoc(module, pageId, toModulePath(path)),
    onSuccess: (_, path) => {
      if (selectedPath === path) {
        setSelectedPath(null);
        setIsNew(false);
      }
      invalidateList();
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      toast.error(`Could not delete file: ${msg}`);
    },
  });

  const deleteFolder = useMutation({
    mutationFn: (path: string) => api.deleteModuleFolder(module, pageId, toModulePath(path)),
    onSuccess: invalidateList,
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      toast.error(`Could not delete folder: ${msg}`);
    },
  });

  const moveItem = useMutation({
    mutationFn: ({ from, to }: { from: string; to: string }) =>
      api.moveModuleItem(module, pageId, toModulePath(from), toModulePath(to)),
    onSuccess: (_result, { from, to }) => {
      // State is scope-relative; follow the moved doc to its new scope-relative path.
      if (selectedPath === from) setSelectedPath(to);
      setRenamingPath(null);
      setRenameValue("");
      invalidateList();
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.detail : String(err);
      toast.error(`Could not rename: ${msg}`);
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

  const data = listData ?? EditorData.parse({});
  const tree = buildTree(data.docs);

  // Tree search (#339): a client-side filter over the active scope's documents. When a query
  // is present the tree is replaced by a flat list of matching files (title or path match);
  // the box only appears when there are files to search.
  const fileDocs = data.docs.filter((d) => d.type === "file");
  const hasAnyDocs = data.docs.length > 0;
  const filterQuery = treeFilter.trim().toLowerCase();
  const filterMatches = filterQuery
    ? fileDocs.filter(
        (d) =>
          d.title.toLowerCase().includes(filterQuery) ||
          d.path.toLowerCase().includes(filterQuery),
      )
    : [];

  // Scope (knowledge base) state derived from the page data (#KB-refactor).
  const showSwitcher = data.scope_noun !== "";
  const hasProjects = data.scopes.some((s) => s.kind === "project");
  const isReferenceScope = data.scopes.find((s) => s.id === scope)?.kind === "reference";

  const handleSelectScope = (id: string) => {
    if (id === scope) return;
    setActiveScope(id);
    setSelectedPath(null);
    setIsNew(false);
    setNewFolderCreating(false);
    setCreatingProject(false);
    setCollapsed(new Set());
    setTreeFilter("");
  };

  const submitNewProject = (event: FormEvent) => {
    event.preventDefault();
    const name = newProjectName.trim();
    if (!name) return;
    createProject.mutate(name);
  };

  const openDoc = (path: string) => {
    flush(); // (1) leaving the current document — persist it before switching
    setViewingVersion(null);
    setHistoryOpen(false);
    setIsNew(false);
    setSelectedPath(path);
  };

  // can_manage_files: start a new document at the scope root ("New document").
  const handleStartNewDocument = () => {
    const taken = new Set(data.docs.map((d) => d.path));
    const slug = uniqueSlug("new-note.md", taken);
    setViewingVersion(null);
    setHistoryOpen(false);
    setIsNew(true);
    setSelectedPath(slug);
    setDraft("");
    setBaseline("");
    setMode("edit");
  };

  // Restore a viewed past version (ADR-0046): make its content the live buffer and save it
  // as a new version through the normal path. Confirms first (in-theme, #488) if it would
  // drop unsaved edits.
  const applyRestore = () => {
    if (!viewingVersion || !selectedPath) return;
    const content = viewingVersion.content;
    setConfirmingRestore(false);
    setViewingVersion(null);
    setDraft(content);
    setMode("preview");
    save.mutate({ path: selectedPath, content });
  };
  const restoreVersion = () => {
    if (!viewingVersion || !selectedPath || data.read_only) return;
    if (dirty) {
      setConfirmingRestore(true);
      return;
    }
    applyRestore();
  };

  // can_create: the existing "New note" flow (used by Notes module)
  const startNewNote = (event: FormEvent) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    const folderPrefix = newFileInFolder ? `${newFileInFolder}/` : "";
    const taken = new Set(data.docs.map((d) => d.path));
    const slug = uniqueSlug(folderPrefix + slugify(name), taken);
    setViewingVersion(null);
    setHistoryOpen(false);
    setIsNew(true);
    setSelectedPath(slug);
    setSeededPath(slug); // the buffer is authoritative; don't reseed when the save refetches
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
    setViewingVersion(null);
    setHistoryOpen(false);
    setIsNew(true);
    setSelectedPath(slug);
    setSeededPath(slug); // the buffer is authoritative; don't reseed when the save refetches
    setDraft("");
    setBaseline("");
    setMode("edit");
  };

  const handleDeleteFile = (path: string) => setPathToDelete(path);

  const handleDeleteFolder = (path: string) => setFolderToDelete(path);

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
    <div className="grid h-full min-h-0 min-w-0 sm:grid-cols-[minmax(0,18rem)_1fr]">
      {/* document list — hidden on phone once a document is open */}
      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-col overflow-y-auto overscroll-contain border-edge sm:border-r",
          selectedPath && "hidden sm:flex",
        )}
      >
        {/* knowledge-base switcher (#KB-refactor) — only when the page declares scopes */}
        {showSwitcher && (
          <>
            <ScopeSwitcher
              scopes={data.scopes}
              active={scope}
              noun={data.scope_noun}
              canCreate={data.can_create_scope}
              canDelete={data.can_create_scope}
              creating={createProject.isPending}
              onSelect={handleSelectScope}
              onStartCreate={() => {
                setCreatingProject(true);
                setNewProjectName("");
              }}
              onDelete={(s) => setScopeToDelete(s)}
            />
            <Confirm
              open={scopeToDelete !== null}
              danger
              message={
                scopeToDelete
                  ? `Delete the ${data.scope_noun} “${scopeToDelete.title}” and all its documents? This cannot be undone.`
                  : ""
              }
              confirmLabel="Delete"
              onCancel={() => setScopeToDelete(null)}
              onConfirm={() => scopeToDelete && deleteProject.mutate(scopeToDelete.id)}
            />
            {creatingProject && (
              <form
                onSubmit={submitNewProject}
                className="flex items-center gap-2 border-b border-edge p-2"
              >
                <TextInput
                  autoFocus
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      setCreatingProject(false);
                      setNewProjectName("");
                    }
                  }}
                  placeholder={`New ${data.scope_noun}…`}
                  aria-label={`New ${data.scope_noun} name`}
                />
                <Button
                  type="submit"
                  variant="primary"
                  disabled={!newProjectName.trim()}
                  busy={createProject.isPending}
                >
                  Create
                </Button>
              </form>
            )}
          </>
        )}

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
              <div className="flex items-center gap-1.5">
                <Button
                  variant="ghost"
                  className="flex-1 justify-start"
                  onClick={handleStartNewDocument}
                >
                  <FilePlus size={15} /> New document
                </Button>
                <Button
                  variant="ghost"
                  className="shrink-0 px-2"
                  onClick={() => setNewFolderCreating(true)}
                  title="New folder"
                  aria-label="New folder"
                >
                  <Folder size={15} />
                </Button>
              </div>
            )}
          </div>
        )}

        {/* search box (#339) — filter the active scope's documents by title/path */}
        {fileDocs.length > 0 && (
          <div className="border-b border-edge p-2">
            <div className="relative flex items-center">
              <Search size={13} className="pointer-events-none absolute left-2 text-ink-faint" />
              <TextInput
                value={treeFilter}
                onChange={(e) => setTreeFilter(e.target.value)}
                placeholder="Search documents…"
                aria-label="Search documents"
                className="h-8 w-full pl-7 pr-7"
              />
              {treeFilter && (
                <button
                  type="button"
                  onClick={() => setTreeFilter("")}
                  aria-label="Clear search"
                  className="absolute right-2 text-ink-faint hover:text-ink"
                >
                  <X size={13} />
                </button>
              )}
            </div>
          </div>
        )}

        {!hasAnyDocs ? (
          showSwitcher && !hasProjects ? (
            <EmptyState quote={`No ${data.scope_noun}s yet — create one to begin.`} />
          ) : isReferenceScope ? (
            <EmptyState quote="No documents in this reference set." />
          ) : data.can_manage_files ? (
            <EmptyState quote="No documents yet — add a document or a folder." />
          ) : data.can_create ? (
            <EmptyState quote="No notes yet — create one with New note." />
          ) : (
            <EmptyState quote="An empty vault. Add notes in Obsidian and they appear here." />
          )
        ) : filterQuery ? (
          filterMatches.length > 0 ? (
            <ul className="flex flex-col p-2">
              {filterMatches.map((d) => (
                <li key={d.id}>
                  <button
                    onClick={() => openDoc(d.path)}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-(--radius-field) px-2 py-1.5 text-left transition-colors",
                      d.path === selectedPath
                        ? "bg-accent-dim text-accent-strong"
                        : "text-ink hover:bg-surface-2",
                    )}
                  >
                    <FileText size={14} className="shrink-0 text-ink-faint" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm">{d.title}</span>
                      {d.path !== d.title && (
                        <span className="block truncate text-xs text-ink-faint">{d.path}</span>
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState quote="No matching documents." />
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
      <div className={cn("flex min-h-0 min-w-0 flex-col", !selectedPath && "hidden sm:flex")}>
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
            {/* toolbar — pinned above the scrolling body so it never scrolls away */}
            <div className="flex shrink-0 items-center gap-2 border-b border-edge px-3 py-2">
              <button
                onClick={() => {
                  flush(); // (1) leaving the document — persist it before closing
                  setSelectedPath(null);
                  setIsNew(false);
                }}
                className="inline-flex shrink-0 items-center gap-1 text-sm text-ink-dim hover:text-ink sm:hidden"
              >
                <ChevronLeft size={15} /> back
              </button>
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-dim">
                {selectedPath}
                {isNew && <span className="ml-1 text-ink-faint">(new)</span>}
              </span>
              {/* Editing controls — hidden while previewing a past version (ADR-0046). */}
              {!viewingVersion && (
                <>
                  {/* Save status: ADR-0042 keeps this live; the button is an explicit flush. */}
                  {save.isPending ? (
                    <span className="shrink-0 text-xs text-ink-faint">Saving…</span>
                  ) : save.isError ? (
                    <Badge tone="danger">save failed</Badge>
                  ) : dirty ? (
                    <span className="shrink-0 text-xs text-ink-faint">Unsaved…</span>
                  ) : save.isSuccess && !save.data?.indexed ? (
                    <Badge tone="warn">saved · not indexed</Badge>
                  ) : save.isSuccess ? (
                    <Badge tone="ok">saved</Badge>
                  ) : null}
                  <div className="inline-flex shrink-0 overflow-hidden rounded-(--radius-field) border border-edge">
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
                </>
              )}

              {/* History (ADR-0046): browse + restore past saves of this document. */}
              {versioned && !isNew && (
                <div className="relative shrink-0">
                  <button
                    onClick={() => setHistoryOpen((o) => !o)}
                    title="Version history"
                    aria-label="Version history"
                    className={cn(
                      "inline-flex items-center rounded-(--radius-field) border border-edge px-2 py-1.5 transition-colors",
                      historyOpen
                        ? "bg-accent-dim text-accent-strong"
                        : "text-ink-dim hover:bg-surface-2",
                    )}
                  >
                    <History size={14} />
                  </button>
                  {historyOpen && (
                    <>
                      <button
                        type="button"
                        aria-hidden
                        tabIndex={-1}
                        className="fixed inset-0 z-10 cursor-default"
                        onClick={() => setHistoryOpen(false)}
                      />
                      <div className="absolute right-0 top-full z-20 mt-1 max-h-80 w-72 overflow-y-auto overscroll-contain rounded-(--radius-card) border border-edge bg-surface py-1 shadow-(--ep-shadow)">
                        <div className="px-3 py-1.5 text-xs font-medium text-ink-dim">
                          Version history
                        </div>
                        {versions.isLoading ? (
                          <div className="flex justify-center py-4">
                            <Spinner />
                          </div>
                        ) : versions.isError ? (
                          <p className="px-3 py-3 text-xs text-ink-dim">Couldn’t load history.</p>
                        ) : (versions.data?.versions.length ?? 0) === 0 ? (
                          <p className="px-3 py-3 text-xs text-ink-faint">No past versions yet.</p>
                        ) : (
                          versions.data?.versions.map((v) => (
                            <button
                              key={v.version_id}
                              onClick={() => viewVersion.mutate(v.version_id)}
                              className="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-xs hover:bg-surface-2"
                            >
                              <span className="text-ink">
                                {relativeTime(new Date(v.created_at))}
                              </span>
                              <span className="shrink-0 text-ink-faint">
                                {v.size.toLocaleString()} ch
                              </span>
                            </button>
                          ))
                        )}
                      </div>
                    </>
                  )}
                </div>
              )}

              {/* A watched external vault is read-only here (#232) — Obsidian is the author. */}
              {!viewingVersion &&
                (data.read_only ? (
                  <Badge tone="dim">read-only</Badge>
                ) : (
                  <Button
                    variant="primary"
                    className="shrink-0"
                    onClick={() => save.mutate({ path: selectedPath, content: draft })}
                    disabled={!dirty}
                    busy={save.isPending}
                  >
                    Save
                  </Button>
                ))}
            </div>

            {/* read-only banner: platform docs (reference scope) or an externally-owned vault
                (#232) — hidden while viewing a past version (ADR-0046). */}
            {data.read_only &&
              !viewingVersion &&
              (isReferenceScope ? (
                <div className="border-b border-edge bg-surface-2 px-3 py-1.5 text-xs text-ink-dim">
                  Platform documentation — read-only reference, bundled with the app.
                </div>
              ) : (
                <div className="border-b border-edge bg-surface-2 px-3 py-1.5 text-xs text-ink-dim">
                  Read-only — this vault is managed externally (Obsidian Sync). Edit notes in
                  Obsidian; changes sync back and re-index here automatically.
                </div>
              ))}

            {/* viewing a past version (ADR-0046): a read-only preview with restore / close */}
            {viewingVersion && (
              <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-edge bg-surface-2 px-3 py-1.5 text-xs text-ink-dim">
                <span>
                  Viewing a version from{" "}
                  <span className="text-ink">
                    {relativeTime(new Date(viewingVersion.created_at))}
                  </span>{" "}
                  — read-only.
                </span>
                <div className="ml-auto flex shrink-0 items-center gap-2">
                  {!data.read_only && (
                    <Button
                      variant="primary"
                      className="h-7 px-2.5 py-0 text-xs"
                      onClick={restoreVersion}
                      busy={save.isPending}
                    >
                      Restore this version
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    className="h-7 px-2.5 py-0 text-xs"
                    onClick={() => setViewingVersion(null)}
                  >
                    Close
                  </Button>
                </div>
              </div>
            )}

            {/* body — the sole scroller; `overscroll-contain` keeps a phone's momentum
                scroll from chaining into the bottom tab bar (the "lower panel") */}
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
              {viewingVersion ? (
                <div className="mx-auto max-w-2xl px-5 py-4">
                  <Markdown>{viewingVersion.content}</Markdown>
                </div>
              ) : mode === "edit" ? (
                <TextArea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
                      e.preventDefault();
                      if (!data.read_only && dirty) save.mutate({ path: selectedPath, content: draft });
                    }
                  }}
                  readOnly={data.read_only}
                  spellCheck={false}
                  className="h-full min-h-full overscroll-contain rounded-none border-0 bg-transparent font-mono text-[13px] leading-relaxed focus:border-0"
                  aria-label={`Edit ${selectedPath}`}
                />
              ) : data.read_only ? (
                // A reference / watched (Obsidian) vault is read-only — render it, don't edit.
                <div className="mx-auto max-w-2xl px-5 py-4">
                  <Markdown>{draft}</Markdown>
                </div>
              ) : (
                // Preview is an editable WYSIWYG surface (#377): edits flow back to `draft`
                // (markdown) and the existing idle/leave auto-save (ADR-0042). Keyed on the
                // document so switching docs reseeds the editor; lazy so it stays out of bundle.
                <div className="mx-auto w-full max-w-2xl px-5 py-4">
                  <Suspense
                    fallback={
                      <div className="flex justify-center py-8">
                        <Spinner />
                      </div>
                    }
                  >
                    <WysiwygEditor
                      key={`${scope}:${selectedPath}`}
                      value={draft}
                      onChange={setDraft}
                    />
                  </Suspense>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* Destructive tree actions confirm in-theme (#488) — never via window.confirm. */}
      <Confirm
        open={pathToDelete !== null}
        danger
        message={pathToDelete ? `Delete “${pathToDelete}”? This cannot be undone.` : ""}
        confirmLabel="Delete"
        onCancel={() => setPathToDelete(null)}
        onConfirm={() => {
          if (pathToDelete) deleteDoc.mutate(pathToDelete);
          setPathToDelete(null);
        }}
      />
      <Confirm
        open={folderToDelete !== null}
        danger
        message={folderToDelete ? `Delete the folder “${folderToDelete}”? It must be empty.` : ""}
        confirmLabel="Delete"
        onCancel={() => setFolderToDelete(null)}
        onConfirm={() => {
          if (folderToDelete) deleteFolder.mutate(folderToDelete);
          setFolderToDelete(null);
        }}
      />
      <Confirm
        open={confirmingRestore}
        danger
        message="Restore this version? Unsaved changes will be replaced."
        confirmLabel="Restore"
        onCancel={() => setConfirmingRestore(false)}
        onConfirm={applyRestore}
      />
    </div>
  );
}
