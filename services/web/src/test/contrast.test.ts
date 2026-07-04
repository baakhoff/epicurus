import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * WCAG AA contrast gate for the --ep-* tokens (#490): every text-role token must hold
 * >= 4.5:1 against ALL THREE backgrounds it can sit on (canvas, surface, surface-2), in
 * both themes — 10–13px muted text is everywhere, so nothing gets the large-text discount.
 * Parses src/index.css directly: a theme tweak that regresses a ratio fails here instead
 * of shipping illegible-on-a-phone-in-daylight text. Decorative/disabled uses are exempt
 * by convention, but the tokens themselves stay compliant so no call site has to care.
 */

const css = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "..", "index.css"),
  "utf8",
);

function tokensIn(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/--ep-([a-z0-9-]+):\s*(#[0-9a-fA-F]{6,8})\b/g)) {
    out[m[1]] = m[2];
  }
  return out;
}

function block(afterSelector: string): string {
  const start = css.indexOf(afterSelector);
  if (start < 0) throw new Error(`selector not found: ${afterSelector}`);
  return css.slice(start, css.indexOf("}", start));
}

const darkTokens = tokensIn(block(":root {"));
// The light theme inherits :root and overrides — merge to get its effective values.
const lightTokens = { ...darkTokens, ...tokensIn(block(':root[data-theme="light"]')) };

function channel(hex: string, i: number): number {
  return parseInt(hex.slice(1 + 2 * i, 3 + 2 * i), 16);
}

function luminance(hex: string): number {
  const lin = (v: number) => {
    const c = v / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(channel(hex, 0)) + 0.7152 * lin(channel(hex, 1)) + 0.0722 * lin(channel(hex, 2));
}

function ratio(fg: string, bg: string): number {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
}

/** Composite an 8-digit #rrggbbaa over an opaque background (the accent-dim badge fill). */
function blendOver(rgba: string, under: string): string {
  const alpha = parseInt(rgba.slice(7, 9), 16) / 255;
  const mix = (i: number) =>
    Math.round(alpha * channel(rgba, i) + (1 - alpha) * channel(under, i));
  return `#${[0, 1, 2].map((i) => mix(i).toString(16).padStart(2, "0")).join("")}`;
}

const THEMES: Array<[string, Record<string, string>]> = [
  ["dark", darkTokens],
  ["light", lightTokens],
];
const BACKGROUNDS = ["canvas", "surface", "surface-2"] as const;
const TEXT_ROLES = ["text", "text-dim", "text-faint", "ok", "warn", "danger"] as const;

describe.each(THEMES)("%s theme contrast (WCAG AA, #490)", (_name, tokens) => {
  it.each(TEXT_ROLES.map((r) => [r] as const))(
    "--ep-%s holds >= 4.5:1 on every background",
    (role) => {
      for (const bg of BACKGROUNDS) {
        const r = ratio(tokens[role], tokens[bg]);
        expect(r, `--ep-${role} ${tokens[role]} on --ep-${bg} ${tokens[bg]} = ${r.toFixed(2)}`)
          .toBeGreaterThanOrEqual(4.5);
      }
    },
  );

  it("keeps the muted hierarchy: faint reads quieter than dim, dim quieter than text", () => {
    const on = (fg: string) => ratio(fg, tokens["canvas"]);
    expect(on(tokens["text-faint"])).toBeLessThan(on(tokens["text-dim"]));
    expect(on(tokens["text-dim"])).toBeLessThan(on(tokens["text"]));
  });

  // The accent Badge (`text-accent-strong` on `bg-accent-dim`): its worst real background
  // is the translucent accent fill composited over surface-2 (a hovered row). Both accent
  // families must clear it — paused (moon) swaps in at runtime via data-power (#490).
  it.each([["gold"], ["moon"]])(
    "%s-strong stays legible on its own dim fill over surface-2",
    (accent) => {
      const worst = blendOver(tokens[`${accent}-dim`], tokens["surface-2"]);
      const r = ratio(tokens[`${accent}-strong`], worst);
      expect(r, `--ep-${accent}-strong on ${worst} = ${r.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
    },
  );
});
