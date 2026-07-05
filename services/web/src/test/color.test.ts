import { describe, expect, it } from "vitest";

import { contrastRatio, onColor, relativeLuminance } from "@/lib/color";

/**
 * The dynamic on-colour pick (#531): unlike the static tokens gated by contrast.test.ts,
 * calendar colours arrive at runtime, so the AA guarantee has to hold for *whole families*
 * of inputs — every provider hex we can think of and every hue `calendarColor()` derives.
 */
describe("onColor (#531)", () => {
  it("computes WCAG luminance and contrast for hex colours", () => {
    expect(relativeLuminance("#ffffff")).toBeCloseTo(1, 5);
    expect(relativeLuminance("#000000")).toBeCloseTo(0, 5);
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 3);
    // Symmetric — argument order must not matter.
    expect(contrastRatio("#af4fd7", "#ffffff")).toBeCloseTo(
      contrastRatio("#ffffff", "#af4fd7") ?? NaN,
      10,
    );
  });

  it("parses short hex and the hsl() form calendarColor() derives", () => {
    expect(relativeLuminance("#fff")).toBeCloseTo(1, 5);
    expect(relativeLuminance("#000")).toBeCloseTo(0, 5);
    // hsl with 0% saturation is pure grey — lightness 50% ≈ #808080.
    const grey = relativeLuminance("hsl(0 0% 50%)");
    expect(grey).not.toBeNull();
    expect(grey).toBeCloseTo(relativeLuminance("#808080") ?? NaN, 2);
  });

  it("picks dark ink on light fills and white on dark fills", () => {
    expect(onColor("#fbd75b")).toBe("#121411"); // banana yellow → house ink
    expect(onColor("#e8e6dd")).toBe("#121411"); // near-white → house ink
    expect(onColor("#1a1d17")).toBe("#ffffff"); // near-black → white
    expect(onColor("#3f51b5")).toBe("#ffffff"); // indigo → white
  });

  it("escapes to pure black in the crossover band where neither house colour clears AA", () => {
    // #af4fd7 (the CalendarView fixture): ink = 4.34:1, white = 4.27:1 — both short of
    // 4.5. Pure black is the guaranteed-compliant fallback (4.92:1 here).
    expect(onColor("#af4fd7")).toBe("#000000");
    expect(contrastRatio("#000000", "#af4fd7")).toBeGreaterThanOrEqual(4.5);
  });

  it("holds AA (>= 4.5:1) against every hue calendarColor() can derive", () => {
    // calendarColor(id) emits `hsl(h 55% 58%)` for h in 0..359 — the full fallback space.
    for (let h = 0; h < 360; h++) {
      const fill = `hsl(${h} 55% 58%)`;
      const ratio = contrastRatio(onColor(fill), fill);
      expect(ratio, `${fill} → ${onColor(fill)} = ${ratio?.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("holds AA against the classic Google event palette", () => {
    const google = [
      "#a4bdfc", "#7ae7bf", "#dbadff", "#ff887c", "#fbd75b", "#ffb878",
      "#46d6db", "#e1e4e8", "#5484ed", "#51b749", "#dc2127",
      "#7986cb", "#33b679", "#8e24aa", "#e67c73", "#f6bf26", "#f4511e",
      "#039be5", "#616161", "#3f51b5", "#0b8043", "#d50000",
    ];
    for (const fill of google) {
      const ratio = contrastRatio(onColor(fill), fill);
      expect(ratio, `${fill} → ${onColor(fill)} = ${ratio?.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("falls back to house ink on colours it cannot parse", () => {
    expect(relativeLuminance("tomato")).toBeNull();
    expect(relativeLuminance("var(--cal)")).toBeNull();
    expect(contrastRatio("tomato", "#ffffff")).toBeNull();
    expect(onColor("tomato")).toBe("#121411");
    expect(onColor("")).toBe("#121411");
  });
});
