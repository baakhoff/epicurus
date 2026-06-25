/**
 * Recommend a KV-cache type from the detected hardware — the same web-side, hardware-aware
 * hint pattern as `modelFit.ts`, computed from the already-fetched `SystemInfo`. The attention
 * cache quantization trades a little quality for VRAM headroom, so the right pick depends on
 * how much VRAM you have. Deliberately rough: a sensible starting point, not a measured
 * optimum (#329).
 */
import type { SystemInfo } from "@/lib/contracts";

const GB = 1024; // MB per GB

export interface KvCacheRecommendation {
  /** Matches the KV_CACHE_OPTIONS values: "" = default f16, else "q8_0" | "q4_0". */
  value: "" | "q8_0" | "q4_0";
  /** Short human name for the suggestion hint ("Default (f16)" / "q8_0" / "q4_0"). */
  name: string;
  /** One-sentence explanation for the hint. */
  reason: string;
}

function gb(mb: number): string {
  return `${(mb / GB).toFixed(mb < 10 * GB ? 1 : 0)} GB`;
}

/**
 * Suggest a KV-cache type for the detected hardware. Returns `null` when there's nothing to
 * judge against (no system info yet), so the caller simply shows no hint.
 */
export function recommendKvCache(system: SystemInfo | undefined): KvCacheRecommendation | null {
  if (!system) return null;
  const vram = system.gpu?.vram_total_mb ?? null;

  // No GPU → CPU inference: quantizing the cache costs quality for little gain.
  if (!vram || vram <= 0) {
    return {
      value: "",
      name: "Default (f16)",
      reason: "No GPU detected — on CPU the full-precision cache keeps quality at little cost.",
    };
  }
  // Ample VRAM: no need to trade quality for room.
  if (vram >= 16 * GB) {
    return {
      value: "",
      name: "Default (f16)",
      reason: `${gb(vram)} of VRAM is ample — keep the full-precision (f16) cache for best quality.`,
    };
  }
  // Moderate VRAM: q8_0 halves the cache with a negligible quality cost.
  if (vram >= 8 * GB) {
    return {
      value: "q8_0",
      name: "q8_0",
      reason: `${gb(vram)} of VRAM is moderate — q8_0 halves the cache for a longer context at a tiny quality cost.`,
    };
  }
  // Tight VRAM: q4_0 quarters the cache so a usable context still fits.
  return {
    value: "q4_0",
    name: "q4_0",
    reason: `${gb(vram)} of VRAM is tight — q4_0 quarters the cache so a usable context still fits.`,
  };
}
