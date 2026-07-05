/**
 * The shell's toast stack (#488) — cards in the UpdateToast idiom, themed via the
 * --ep-* tokens. Each card is a `role="status"` live region (implicit polite
 * announcement) with a manual close; errors linger longer than info before
 * auto-dismissing. The cards render as flow children of the shell's CornerStack
 * (#510) — the region owns the corner and its above-Confirm z order, so no fixed
 * pinning here.
 */
import { useEffect } from "react";
import { CircleAlert, Info, X } from "lucide-react";

import { cn } from "@/components/ui";
import { useToasts, type Toast } from "@/stores/toasts";

const TOAST_MS: Record<Toast["tone"], number> = { error: 8000, info: 4000 };

function ToastCard({ toast }: { toast: Toast }) {
  const dismiss = useToasts((s) => s.dismiss);

  useEffect(() => {
    const id = window.setTimeout(() => dismiss(toast.id), TOAST_MS[toast.tone]);
    return () => window.clearTimeout(id);
  }, [toast.id, toast.tone, dismiss]);

  const Icon = toast.tone === "error" ? CircleAlert : Info;
  return (
    <div
      role="status"
      className={cn(
        "pointer-events-auto flex items-start gap-2.5 rounded-(--radius-card) border bg-surface p-3 shadow-(--ep-shadow)",
        toast.tone === "error" ? "border-danger/40" : "border-edge",
      )}
    >
      <Icon
        size={16}
        className={cn("mt-0.5 shrink-0", toast.tone === "error" ? "text-danger" : "text-ink-dim")}
      />
      <p className="min-w-0 flex-1 text-sm text-ink">{toast.message}</p>
      <button
        onClick={() => dismiss(toast.id)}
        aria-label="Dismiss"
        className="-m-0.5 rounded-md p-0.5 text-ink-dim hover:bg-surface-2 hover:text-ink"
      >
        <X size={15} />
      </button>
    </div>
  );
}

export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  return (
    <>
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} />
      ))}
    </>
  );
}
