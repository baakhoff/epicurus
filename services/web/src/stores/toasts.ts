/**
 * Transient shell notifications (#488) — the themed replacement for `window.alert`.
 * Store-driven so any code path (a mutation's onError, a store, a plain helper) can
 * raise one without hooks; the single <Toaster/> in the shell renders the stack.
 */
import { create } from "zustand";

export type ToastTone = "error" | "info";

export interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastStore {
  toasts: Toast[];
  push: (tone: ToastTone, message: string) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToasts = create<ToastStore>()((set) => ({
  toasts: [],
  push: (tone, message) =>
    set((s) => ({
      // Re-raising an identical message replaces the old card (fresh id, fresh
      // auto-dismiss clock) instead of stacking duplicates — a retried mutation
      // must not fill the screen with copies of the same failure.
      toasts: [
        ...s.toasts.filter((t) => !(t.tone === tone && t.message === message)),
        { id: nextId++, tone, message },
      ],
    })),
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

/** Imperative surface for non-hook call sites: `toast.error("Could not save.")`. */
export const toast = {
  error: (message: string) => useToasts.getState().push("error", message),
  info: (message: string) => useToasts.getState().push("info", message),
};
