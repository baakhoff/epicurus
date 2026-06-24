/**
 * "Good for your system?" — a coarse suitability hint for a model against the detected
 * hardware (from `GET /system/info`). Deliberately rough: a starting-point signal, not a
 * guarantee. Computed in the web from the already-fetched `SystemInfo` (the Models page
 * fetches it once) so the catalog/models endpoints don't probe hardware per request.
 */
import type { SystemInfo } from "@/lib/contracts";

export type FitRating = "good" | "tight" | "offload" | "cpu" | "too_big" | "unknown";

export interface ModelFit {
  rating: FitRating;
  /** Short badge text ("" when unknown — caller hides the badge). */
  label: string;
  /** One-sentence explanation for the hover tooltip. */
  reason: string;
  /** Maps to the shared `Badge` tone. */
  tone: "ok" | "warn" | "danger" | "dim";
}

const GB = 1024; // MB per GB

/**
 * Rough on-disk size (MB) from a params label like `7b`, `3.8b`, or `8x7b` — the upstream
 * catalog often omits a real size. Assumes ~Q4 weights (~0.6 GB per billion params, an
 * intentional over-estimate so the hint errs conservative).
 */
export function estimateSizeMb(params: string | null | undefined): number | null {
  if (!params) return null;
  const moe = params.toLowerCase().match(/([\d.]+)\s*x\s*([\d.]+)\s*b/);
  const plain = params.toLowerCase().match(/([\d.]+)\s*b/);
  let billions: number;
  if (moe) billions = parseFloat(moe[1]) * parseFloat(moe[2]);
  else if (plain) billions = parseFloat(plain[1]);
  else return null;
  if (!isFinite(billions) || billions <= 0) return null;
  return Math.round(billions * 0.6 * GB);
}

function gb(mb: number): string {
  return `${(mb / GB).toFixed(mb < 10 * GB ? 1 : 0)} GB`;
}

/**
 * Rate how well a model fits the detected hardware. `sizeMb` is the known on-disk size
 * (installed models); for a catalog entry pass `null` and the `params` label to estimate.
 * Returns `rating: "unknown"` (empty label) when there's nothing to judge against.
 */
export function assessFit(
  system: SystemInfo | undefined,
  sizeMb: number | null,
  params?: string | null,
): ModelFit {
  const size = sizeMb ?? estimateSizeMb(params);
  if (!system || size == null || size <= 0) {
    return { rating: "unknown", label: "", reason: "", tone: "dim" };
  }
  const vram = system.gpu?.vram_total_mb ?? null;
  const ram = system.ram_total_mb ?? null;

  if (vram && vram > 0) {
    if (size * 1.2 <= vram) {
      return {
        rating: "good",
        label: "Runs on GPU",
        tone: "ok",
        reason: `≈${gb(size)} fits your ${gb(vram)} of VRAM with room for context.`,
      };
    }
    if (size <= vram) {
      return {
        rating: "tight",
        label: "Tight on GPU",
        tone: "warn",
        reason: `≈${gb(size)} barely fits your ${gb(vram)} of VRAM — little room left for a large context.`,
      };
    }
    if (ram && size <= ram * 0.8) {
      return {
        rating: "offload",
        label: "Partly on CPU",
        tone: "warn",
        reason: `≈${gb(size)} is larger than your ${gb(vram)} of VRAM, so it spills to CPU/RAM — expect slower tokens.`,
      };
    }
    return {
      rating: "too_big",
      label: "Too big",
      tone: "danger",
      reason: `≈${gb(size)} exceeds your ${gb(vram)} of VRAM${ram ? ` and ${gb(ram)} of RAM` : ""}.`,
    };
  }

  // No GPU detected → CPU inference.
  if (ram && size <= ram * 0.6) {
    return {
      rating: "cpu",
      label: "CPU only",
      tone: "warn",
      reason: `No GPU detected — runs on CPU from your ${gb(ram)} of RAM; smaller models stay responsive.`,
    };
  }
  if (ram && size <= ram * 0.85) {
    return {
      rating: "tight",
      label: "Heavy on CPU",
      tone: "warn",
      reason: `≈${gb(size)} fits your ${gb(ram)} of RAM but is heavy for CPU inference — expect slow tokens.`,
    };
  }
  if (ram) {
    return {
      rating: "too_big",
      label: "Too big",
      tone: "danger",
      reason: `≈${gb(size)} is too large for your ${gb(ram)} of RAM.`,
    };
  }
  return { rating: "unknown", label: "", reason: "", tone: "dim" };
}
