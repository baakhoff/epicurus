/**
 * WYSIWYG markdown editor for the `editor` archetype's Preview (#377).
 *
 * Wraps Milkdown's **Crepe** — a markdown-native rich editor (ProseMirror + remark) — so the
 * rendered note is *editable in place*, not read-only. Markdown stays the source of truth: the
 * editor parses the document's markdown on open and serializes back to markdown on every edit,
 * feeding the parent's existing `draft` → idle/leave auto-save → version flow unchanged
 * (ADR-0042). The save contract (`PUT …/doc {content}`) is untouched.
 *
 * Crepe is framework-agnostic (no React adapter — so no React-version peer-dep risk), so it is
 * mounted imperatively on a host div. It is **uncontrolled** after mount: the parent re-keys
 * this component on the document path, so switching documents remounts it with fresh content
 * rather than resetting a live editor (which would fight the cursor). It is lazy-loaded by the
 * parent so the heavy editor never enters the main bundle.
 *
 * Ownership guard (#781): the parent must never mount this component before the incoming
 * document's content has actually replaced `value` (see EditorView's seed gate) — otherwise
 * `defaultValue` silently starts Crepe from the *outgoing* document. As defense in depth against
 * that precondition somehow arising anyway, this component reports the `docKey` it was mounted
 * for with every `onChange`, captured once at mount and never re-read, so the parent can always
 * attribute — and drop — a write from a surface that is stale, however it came to be so.
 */
import { Crepe } from "@milkdown/crepe";
import { useEffect, useRef } from "react";

import "@milkdown/crepe/theme/common/style.css";
import "@milkdown/crepe/theme/nord-dark.css";
// Loaded last so `.ep-wysiwyg .milkdown` overrides the theme's `.milkdown` defaults.
import "./WysiwygEditor.css";

export interface WysiwygEditorProps {
  /** The document's markdown at open. Read once — the editor is uncontrolled after mount. */
  value: string;
  /** Identity of the document this instance is mounted for (#781) — e.g. the selected path.
   *  Read once, at mount, and echoed back unchanged with every `onChange`; never re-read after
   *  mount, so a report always names the document Crepe actually holds, not whatever the parent
   *  currently has selected. */
  docKey: string;
  /** Fired with the mount-time `docKey` and the serialized markdown after each *edit* — never
   *  for the initial load, so opening a document never marks it dirty. The parent must drop any
   *  report whose `docKey` no longer names the live document (#781). */
  onChange: (docKey: string, markdown: string) => void;
  /** Render without editing (a watched/reference vault). */
  readOnly?: boolean;
}

export default function WysiwygEditor({
  value,
  docKey,
  onChange,
  readOnly = false,
}: WysiwygEditorProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  // A live ref so the once-only mount effect always calls the current onChange (no stale
  // closure). Synced in an effect, never assigned during render (react-hooks/refs).
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    // Gate edits until the editor is live, so the initial parse never echoes back as a change.
    let live = false;
    const crepe = new Crepe({ root: host, defaultValue: value });
    crepe.on((listener) => {
      listener.markdownUpdated((_ctx, markdown) => {
        // `docKey` is this effect's own closure, fixed at mount (#781) — never the latest
        // prop — so every report names the document Crepe actually holds, even long after a
        // remount has moved `onChangeRef` on to a fresher closure.
        if (live) onChangeRef.current(docKey, markdown);
      });
    });
    void crepe
      .create()
      .then(() => {
        if (disposed) {
          void crepe.destroy();
          return;
        }
        crepe.setReadonly(readOnly);
        live = true;
      })
      .catch(() => {
        // A failed editor init must not crash the screen — the Edit (raw source) tab still works.
      });
    return () => {
      disposed = true;
      void crepe.destroy();
    };
    // Mount once; the parent re-keys on the document path to reseed. `value` / `docKey` /
    // `onChange` after mount are intentionally excluded — the editor is uncontrolled, `docKey`
    // must stay pinned to its mount-time identity, and onChange is a live ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div ref={hostRef} className="ep-wysiwyg" />;
}
