/**
 * Runtime colour contrast (#531) — the #505 "on-accent" discipline applied to colours
 * the app only learns at runtime. Static fills (the gold/moon accents) carry hand-tuned
 * `--ep-on-*` tokens enforced by `contrast.test.ts`; a calendar's colour arrives from
 * provider data, so the readable text colour for a chip filled with it has to be
 * computed per colour instead of frozen into a token.
 *
 * Understood inputs are the two shapes calendars actually produce: hex (`#rgb` /
 * `#rrggbb`, the provider's own colour) and the modern-syntax `hsl(h s% l%)` emitted by
 * `calendarColor()` for calendars without one.
 */

/** The house near-black — the dark canvas hex, same value `--ep-on-gold` uses (#505). */
const INK = "#121411";
const WHITE = "#ffffff";
const BLACK = "#000000";

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  // CSS Color 4 reference algorithm; h in degrees, s/l in [0,1].
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
}

/** Parse a colour into [r,g,b] 0–255, or null when the shape isn't one we understand. */
function parseColor(color: string): [number, number, number] | null {
  const c = color.trim();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c);
  if (hex) {
    const raw = hex[1];
    const full = raw.length === 3 ? raw.split("").map((ch) => ch + ch).join("") : raw;
    return [0, 1, 2].map((i) => parseInt(full.slice(2 * i, 2 * i + 2), 16)) as [
      number,
      number,
      number,
    ];
  }
  const hsl = /^hsl\(\s*(-?[\d.]+)(?:deg)?[\s,]+([\d.]+)%[\s,]+([\d.]+)%\s*\)$/i.exec(c);
  if (hsl) {
    const h = ((parseFloat(hsl[1]) % 360) + 360) % 360;
    return hslToRgb(h, parseFloat(hsl[2]) / 100, parseFloat(hsl[3]) / 100);
  }
  return null;
}

/** WCAG relative luminance of a colour, or null when it can't be parsed. */
export function relativeLuminance(color: string): number | null {
  const rgb = parseColor(color);
  if (!rgb) return null;
  const lin = (v: number) => {
    const c = v / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]);
}

/** WCAG contrast ratio between two colours (1–21), or null when either fails to parse. */
export function contrastRatio(a: string, b: string): number | null {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  if (la == null || lb == null) return null;
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * The readable text colour for a dynamic fill: the first of house ink → white → pure
 * black to clear WCAG AA (4.5:1) against it. Ink and white keep the house palette in
 * play; pure black is the escape hatch for mid-tone fills where *neither* quite clears
 * 4.5 (the black/white pair mathematically guarantees one of them ≥ 4.5, so the pick
 * always lands AA). Unparseable input falls back to ink — the pre-#531 dark-theme
 * behaviour for the fills we can't reason about.
 */
export function onColor(fill: string): string {
  for (const candidate of [INK, WHITE, BLACK]) {
    const ratio = contrastRatio(candidate, fill);
    if (ratio == null) return INK;
    if (ratio >= 4.5) return candidate;
  }
  return WHITE; // unreachable: max(black, white) ≥ 4.58 for every luminance
}
