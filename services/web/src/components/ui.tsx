/**
 * The shell's small primitive kit — shadcn-style, retokened to --ep-*.
 * Deliberately compact: one file, no variants framework, plain Tailwind.
 */
import {
  createContext,
  useContext,
  useEffect,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { Loader2, X } from "lucide-react";

export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/* ── Button ─────────────────────────────────────────────────────────────── */

type ButtonVariant = "primary" | "ghost" | "outline" | "danger";

const buttonStyles: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-canvas font-medium hover:bg-accent-strong disabled:opacity-50",
  ghost: "text-ink-dim hover:text-ink hover:bg-surface-2",
  outline:
    "border border-edge-strong text-ink hover:border-accent hover:text-accent-strong",
  danger: "border border-danger/40 text-danger hover:bg-danger/10",
};

export function Button({
  variant = "outline",
  busy = false,
  className,
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; busy?: boolean }) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-(--radius-field) px-3.5 py-2 text-sm",
        "transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        buttonStyles[variant],
        className,
      )}
      disabled={disabled || busy}
      {...rest}
    >
      {busy && <Loader2 size={15} className="animate-spin" />}
      {children}
    </button>
  );
}

/* ── Badge / dots ───────────────────────────────────────────────────────── */

export function Badge({
  tone = "dim",
  children,
  className,
}: {
  tone?: "dim" | "accent" | "ok" | "warn" | "danger";
  children: ReactNode;
  className?: string;
}) {
  const tones = {
    dim: "border-edge text-ink-dim",
    accent: "border-accent/40 text-accent-strong bg-accent-dim",
    ok: "border-ok/40 text-ok",
    warn: "border-warn/40 text-warn",
    danger: "border-danger/40 text-danger",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] leading-4",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Dot({ tone }: { tone: "ok" | "danger" | "accent" | "dim" }) {
  const tones = {
    ok: "bg-ok",
    danger: "bg-danger",
    accent: "bg-accent",
    dim: "bg-ink-faint",
  };
  return <span className={cn("inline-block size-2 rounded-full", tones[tone])} />;
}

/* ── Card ───────────────────────────────────────────────────────────────── */

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "rounded-(--radius-card) border border-edge bg-surface p-4",
        className,
      )}
    >
      {children}
    </div>
  );
}

/* ── Fields ─────────────────────────────────────────────────────────────── */

const fieldBase =
  "w-full rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2 text-sm text-ink " +
  "placeholder:text-ink-faint focus:border-accent focus:outline-none";

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(fieldBase, props.className)} />;
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn(fieldBase, "resize-none", props.className)} />;
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label?: string;
  disabled?: boolean;
}) {
  // The track colour carries on/off; the thumb is a constant, bright, raised circle
  // in both states ("the same switch, flipped"). A 2px transparent border insets the
  // thumb evenly so it never touches the edge in either position.
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full",
        "border-2 border-transparent transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-accent hover:bg-accent-strong" : "bg-edge-strong hover:bg-ink-faint",
      )}
    >
      <span
        className={cn(
          "pointer-events-none size-5 rounded-full bg-ink shadow-sm",
          "transition-transform duration-200 ease-out",
          checked ? "translate-x-5" : "translate-x-0",
        )}
      />
    </button>
  );
}

export function Label({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="mb-1.5">
      <span className="text-[13px] font-medium text-ink">{children}</span>
      {hint && <p className="mt-0.5 text-xs text-ink-dim">{hint}</p>}
    </div>
  );
}

/* ── Sheet (bottom drawer on phones, side panel on wide screens) ────────── */

const SheetContext = createContext<(() => void) | null>(null);

export function Sheet({
  open,
  onClose,
  title,
  children,
  side = "bottom",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  side?: "bottom" | "left";
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <SheetContext.Provider value={onClose}>
      <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label={title}>
        <div className="absolute inset-0 bg-black/55" onClick={onClose} />
        <div
          className={cn(
            "absolute flex flex-col border-edge bg-surface shadow-(--ep-shadow)",
            side === "bottom"
              ? "inset-x-0 bottom-0 max-h-[85dvh] rounded-t-(--radius-card) border-t pb-safe sm:inset-x-auto sm:right-4 sm:bottom-4 sm:w-105 sm:rounded-(--radius-card) sm:border"
              : "inset-y-0 left-0 w-[88vw] max-w-90 border-r",
          )}
        >
          <header className="flex items-center justify-between border-b border-edge px-4 py-3">
            <h2 className="font-serif text-base text-ink">{title}</h2>
            <button
              onClick={onClose}
              aria-label="Close"
              className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
            >
              <X size={18} />
            </button>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
        </div>
      </div>
    </SheetContext.Provider>
  );
}

export function useCloseSheet(): () => void {
  return useContext(SheetContext) ?? (() => {});
}

/* ── Confirm dialog ─────────────────────────────────────────────────────── */

export function Confirm({
  open,
  message,
  confirmLabel = "Confirm",
  danger = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-60 flex items-center justify-center p-6" role="alertdialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/55" onClick={onCancel} />
      <Card className="relative w-full max-w-sm bg-surface">
        <p className="text-sm text-ink">{message}</p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </Card>
    </div>
  );
}

/* ── Misc ───────────────────────────────────────────────────────────────── */

export function Spinner({ className }: { className?: string }) {
  return <Loader2 size={16} className={cn("animate-spin text-ink-dim", className)} />;
}

export function EmptyState({
  quote,
  children,
}: {
  quote?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 py-14 text-center">
      {quote && (
        <p className="max-w-sm font-serif text-[15px] italic leading-relaxed text-ink-dim">
          {quote}
        </p>
      )}
      {children}
    </div>
  );
}
