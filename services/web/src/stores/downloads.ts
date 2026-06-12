/**
 * Model pulls in flight — global, so a download survives tab switches and
 * renders as a tray pill anywhere in the shell.
 */
import { create } from "zustand";

import { PullProgress } from "@/lib/contracts";
import { sse } from "@/lib/sse";

export interface Download {
  model: string;
  status: string;
  total: number | null;
  completed: number | null;
  error: string | null;
  done: boolean;
}

interface Downloads {
  active: Record<string, Download>;
  pull: (model: string, onFinished: () => void) => Promise<void>;
  dismiss: (model: string) => void;
}

export const useDownloads = create<Downloads>()((set, get) => ({
  active: {},

  pull: async (model, onFinished) => {
    if (get().active[model] && !get().active[model].done) return;
    const update = (patch: Partial<Download>) => {
      const current: Download = get().active[model] ?? {
        model,
        status: "starting",
        total: null,
        completed: null,
        error: null,
        done: false,
      };
      set({ active: { ...get().active, [model]: { ...current, ...patch } } });
    };
    update({ status: "starting", error: null, done: false });
    try {
      for await (const message of sse("/platform/v1/llm/pull/stream", { model })) {
        if (message.event === "progress") {
          const progress = PullProgress.parse(JSON.parse(message.data));
          update({
            status: progress.status || "downloading",
            total: progress.total ?? null,
            completed: progress.completed ?? null,
          });
        } else if (message.event === "error") {
          const { detail } = JSON.parse(message.data) as { detail?: string };
          update({ status: "failed", error: detail ?? "pull failed", done: true });
        } else if (message.event === "done") {
          update({ status: "ready", done: true });
          onFinished();
        }
      }
    } catch (err) {
      update({
        status: "failed",
        error: err instanceof Error ? err.message : "pull failed",
        done: true,
      });
    }
  },

  dismiss: (model) => {
    const active = { ...get().active };
    delete active[model];
    set({ active });
  },
}));
