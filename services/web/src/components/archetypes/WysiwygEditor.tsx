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
  /** Fired with the serialized markdown after each *edit* — never for the initial load, so
   *  opening a document never marks it dirty. */
  onChange: (markdown: string) => void;
  /** Render without editing (a watched/reference vault). */
  readOnly?: boolean;
}

export default function WysiwygEditor({ value, onChange, readOnly = false }: WysiwygEditorProps) {
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
        if (live) onChangeRef.current(markdown);
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
    // Mount once; the parent re-keys on the document path to reseed. `value` / `onChange` after
    // mount are intentionally excluded — the editor is uncontrolled and onChange is a live ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div ref={hostRef} className="ep-wysiwyg" />;
}
