/**
 * The ε mark — a lunate epsilon, drawn as a single stroke. At boot the stroke
 * draws itself in (`.ep-draw`); it doubles as the brand and the app icon.
 */
export function EpsilonMark({ size = 26, draw = false }: { size?: number; draw?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      className="shrink-0"
    >
      <path
        d="M 46 20 A 17.5 17.5 0 1 0 46 44"
        stroke="var(--ep-accent)"
        strokeWidth="6"
        strokeLinecap="round"
        className={draw ? "ep-draw" : undefined}
      />
      <line
        x1="29"
        y1="32"
        x2="45"
        y2="32"
        stroke="var(--ep-accent)"
        strokeWidth="6"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function Wordmark() {
  return (
    <span className="flex items-baseline gap-2">
      <span className="font-serif text-[17px] tracking-wide text-ink">epicurus</span>
    </span>
  );
}
