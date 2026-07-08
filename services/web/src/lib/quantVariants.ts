/**
 * Quant-variant helpers (#330) — order a model's quant variants, size each one, and recommend
 * the best one for the detected hardware. All web-side from data already in hand (the variant
 * list + the model's parameter size + `SystemInfo`), so listing variants costs a single
 * registry call and no per-tag size probing. Since #571 each variant may carry the **real**
 * on-disk size its tags-page row shows; the bits-per-weight estimate is the fallback.
 */
import type { ModelVariant, SystemInfo } from "@/lib/contracts";

const GB = 1024; // MB per GB

// Approx bits-per-weight per quant family, overhead included. Drives the size estimate and the
// quality ordering. Keys are matched as longest-prefix against the lowercased quant label.
const BPW: Record<string, number> = {
  iq1: 1.7,
  iq2: 2.4,
  iq3: 3.2,
  iq4: 4.3,
  q2: 2.6,
  q3: 3.4,
  q4: 4.7,
  q5: 5.6,
  q6: 6.6,
  q8: 8.5,
  fp16: 16,
  bf16: 16,
  f16: 16,
  f32: 32,
};

// Longest-prefix order so "iq4" wins over nothing and "q4" matches "q4_k_m".
const BPW_KEYS = ["iq1", "iq2", "iq3", "iq4", "q2", "q3", "q4", "q5", "q6", "q8", "fp16", "bf16", "f16", "f32"];

// A model's baked default build (empty quant label) is, in practice, ~a Q4_K_M.
const DEFAULT_BPW = 4.7;

/** Approximate bits-per-weight for a quant label ("" = the default build). */
export function quantBitsPerWeight(quant: string): number {
  if (!quant) return DEFAULT_BPW;
  const q = quant.toLowerCase();
  for (const key of BPW_KEYS) {
    if (q.startsWith(key)) return BPW[key];
  }
  return DEFAULT_BPW;
}

/** Parse a parameter-size label ("8.0B", "8b", "270m") to a count in billions. */
function paramsToBillions(label: string | null | undefined): number | null {
  if (!label) return null;
  const match = label.toLowerCase().match(/([\d.]+)\s*([bm])/);
  if (!match) return null;
  const value = parseFloat(match[1]);
  if (!isFinite(value) || value <= 0) return null;
  return match[2] === "m" ? value / 1000 : value;
}

/**
 * Estimate a variant's on-disk size (MB) from the model's parameter count and the quant's
 * bits-per-weight. Rough — a starting-point figure, like the catalog fit hint. Null when the
 * parameter size is unknown.
 */
export function estimateVariantSizeMb(
  paramSize: string | null | undefined,
  quant: string,
): number | null {
  const billions = paramsToBillions(paramSize);
  if (billions == null) return null;
  const bytes = (billions * 1e9 * quantBitsPerWeight(quant)) / 8;
  return Math.round(bytes / (1024 * 1024));
}

/** A tag naming the library's cloud alias ("cloud" / "…-cloud") — no local weights (#571). */
export function isCloudTag(tag: string): boolean {
  return /(^|[:.-])cloud$/.test(tag.toLowerCase());
}

/**
 * A variant's size in MB: the real tags-page size when the core supplied one (#571), the
 * bits-per-weight estimate otherwise — and never an estimate for a cloud alias, which has no
 * local weights to estimate (null keeps its fit badge hidden rather than guessing).
 */
export function variantSizeMb(
  variant: ModelVariant,
  paramSize: string | null | undefined,
): number | null {
  if (variant.size_gb != null) return Math.round(variant.size_gb * 1024);
  if (isCloudTag(variant.tag)) return null;
  return estimateVariantSizeMb(paramSize, variant.quant);
}

/** Variants ordered smallest → largest (by estimated size / bits-per-weight). */
export function sortVariants(variants: ModelVariant[]): ModelVariant[] {
  return [...variants].sort((a, b) => quantBitsPerWeight(a.quant) - quantBitsPerWeight(b.quant));
}

/**
 * The tag to mark "recommended": the highest-quality quant that still fits VRAM with headroom
 * (judged by its real size when known, the estimate otherwise); if none fit, the smallest.
 * With no GPU info, prefer a balanced q4, then the default build. Cloud aliases are never
 * recommended — there's nothing local to run. Null when nothing qualifies.
 */
export function recommendVariantTag(
  variants: ModelVariant[],
  paramSize: string | null | undefined,
  system: SystemInfo | undefined,
): string | null {
  const local = variants.filter((v) => !isCloudTag(v.tag));
  if (local.length === 0) return null;
  const byQualityDesc = [...local].sort(
    (a, b) => quantBitsPerWeight(b.quant) - quantBitsPerWeight(a.quant),
  );
  const vram = system?.gpu?.vram_total_mb ?? null;

  if (vram && vram > 0) {
    for (const v of byQualityDesc) {
      const size = variantSizeMb(v, paramSize);
      if (size != null && size * 1.2 <= vram) return v.tag;
    }
    // Nothing fits comfortably → the smallest is the least-bad choice.
    return byQualityDesc[byQualityDesc.length - 1].tag;
  }

  // No VRAM signal: a q4 is the usual sweet spot; else the default build; else the first.
  const q4 = local.find((v) => v.quant.toLowerCase().startsWith("q4"));
  const def = local.find((v) => v.quant === "");
  return (q4 ?? def ?? local[0]).tag;
}

/** Render a megabyte estimate as a compact "~N GB" string (or "" when unknown). */
export function formatVariantSize(mb: number | null): string {
  if (mb == null || mb <= 0) return "";
  return `~${(mb / GB).toFixed(mb < 10 * GB ? 1 : 0)} GB`;
}
