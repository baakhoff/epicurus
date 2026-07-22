/**
 * The right-panel store (ADR-0018). A core-owned side panel opened programmatically
 * — e.g. from a chat entity-reference click (ADR-0019). Views are a **bounded,
 * core-defined vocabulary**; modules never inject markup, only the data a view binds
 * to. The panel keeps a back-stack so drilling in (and stepping back) is natural.
 */
import { create } from "zustand";

/** The bounded set of views the panel can host. Extends only in core. */
export type PanelView =
  | "entity-detail"
  | "email-reader"
  | "doc-reader"
  | "email-draft"
  /** A document the agent is writing, live beside the chat (#541, ADR-0101). */
  | "document";

export interface PanelEntry {
  view: PanelView;
  /** View-specific data (e.g. a HoverCard for `entity-detail`). */
  payload: unknown;
  /** Header label for this entry. */
  title: string;
}

interface PanelState {
  /** History stack; the last entry is the one on screen. */
  stack: PanelEntry[];
  /** Push a view onto the panel (opening it if closed). */
  open: (view: PanelView, payload: unknown, title?: string) => void;
  /** Swap the on-screen entry's payload in place (keeps its view/back-stack; `title`
   *  defaults to the current entry's own title when omitted). */
  replace: (payload: unknown, title?: string) => void;
  /** Step back one entry; closes the panel when the stack empties. */
  back: () => void;
  /** Close the panel and clear its history. */
  close: () => void;
}

export const usePanel = create<PanelState>()((set, get) => ({
  stack: [],
  open: (view, payload, title = "") =>
    set({ stack: [...get().stack, { view, payload, title }] }),
  replace: (payload, title) =>
    set((s) => {
      if (s.stack.length === 0) return s;
      const stack = s.stack.slice();
      const prev = stack[stack.length - 1];
      // A caller passing a fresh title (e.g. a later frame's own heading) must actually
      // reach the header — the naive `{...prev, payload}` silently kept the *original*
      // entry's title forever (#659). `title ?? prev.title` preserves today's every-caller
      // omits it behavior exactly (still moot: no adopter currently passes one).
      stack[stack.length - 1] = { ...prev, payload, title: title ?? prev.title };
      return { stack };
    }),
  back: () => set({ stack: get().stack.slice(0, -1) }),
  close: () => set({ stack: [] }),
}));

/** The entry currently on screen, or null when the panel is closed. */
export function usePanelCurrent(): PanelEntry | null {
  return usePanel((s) => s.stack[s.stack.length - 1] ?? null);
}

/** How deep the back-stack is (drives the back affordance). */
export function usePanelDepth(): number {
  return usePanel((s) => s.stack.length);
}
