import { describe, expect, it } from "vitest";

import {
  estimateVariantSizeMb,
  isCloudTag,
  quantBitsPerWeight,
  recommendVariantTag,
  sortVariants,
  variantSizeMb,
} from "@/lib/quantVariants";
import type { ModelVariant, SystemInfo } from "@/lib/contracts";

const v = (tag: string, quant: string, size_gb: number | null = null): ModelVariant => ({
  tag,
  quant,
  size_gb,
});

function gpu(vramMb: number): SystemInfo {
  return {
    gpu: { vendor: "nvidia", name: "GPU", vram_total_mb: vramMb, vram_free_mb: vramMb },
    ram_total_mb: 32000,
  };
}

describe("quantBitsPerWeight", () => {
  it("matches quant families by longest prefix", () => {
    expect(quantBitsPerWeight("q4_K_M")).toBe(4.7);
    expect(quantBitsPerWeight("q8_0")).toBe(8.5);
    expect(quantBitsPerWeight("fp16")).toBe(16);
  });

  it("treats the empty (default) build as a q4-ish baseline", () => {
    expect(quantBitsPerWeight("")).toBe(4.7);
  });
});

describe("estimateVariantSizeMb", () => {
  it("scales with parameter count and quant width", () => {
    const q4 = estimateVariantSizeMb("8b", "q4_K_M");
    const q8 = estimateVariantSizeMb("8b", "q8_0");
    expect(q4).not.toBeNull();
    expect(q4!).toBeGreaterThan(3000); // ~4.3 GB for an 8B q4
    expect(q4!).toBeLessThan(6000);
    expect(q8!).toBeGreaterThan(q4!); // a wider quant is bigger
  });

  it("returns null when the parameter size is unknown", () => {
    expect(estimateVariantSizeMb(null, "q4_0")).toBeNull();
  });
});

describe("sortVariants", () => {
  it("orders smallest quant first", () => {
    const sorted = sortVariants([v("m:8b-q8_0", "q8_0"), v("m:8b-q4_0", "q4_0"), v("m:8b-fp16", "fp16")]);
    expect(sorted.map((x) => x.quant)).toEqual(["q4_0", "q8_0", "fp16"]);
  });
});

describe("isCloudTag", () => {
  it("matches the library's cloud aliases", () => {
    expect(isCloudTag("deepseek-v4-flash:cloud")).toBe(true);
    expect(isCloudTag("qwen3-coder:480b-cloud")).toBe(true);
  });

  it("never matches ordinary tags or cloud-ish names", () => {
    expect(isCloudTag("llama3.1:8b")).toBe(false);
    expect(isCloudTag("m:8b-instruct-q4_0")).toBe(false);
    expect(isCloudTag("cloudmodel:7b")).toBe(false); // "cloud" as a name prefix, not an alias
  });
});

describe("variantSizeMb (#571)", () => {
  it("prefers the real tags-page size over the estimate", () => {
    // Real 8.5 GB beats whatever the bpw estimate would say.
    expect(variantSizeMb(v("m:8b-q8_0", "q8_0", 8.5), "8b")).toBe(Math.round(8.5 * 1024));
  });

  it("falls back to the bits-per-weight estimate when no real size is known", () => {
    expect(variantSizeMb(v("m:8b-q8_0", "q8_0"), "8b")).toBe(estimateVariantSizeMb("8b", "q8_0"));
  });

  it("never estimates a cloud alias — there are no local weights", () => {
    expect(variantSizeMb(v("m:480b-cloud", ""), "480b")).toBeNull();
  });
});

describe("recommendVariantTag", () => {
  const variants = [v("m:8b-q4_K_M", "q4_K_M"), v("m:8b-q8_0", "q8_0"), v("m:8b-fp16", "fp16")];

  it("picks the best quality that fits VRAM with headroom", () => {
    // 8B: q8 ≈ 8.1 GB (×1.2 ≈ 9.7 < 12), fp16 ≈ 15 GB (doesn't fit) → q8_0 wins.
    expect(recommendVariantTag(variants, "8b", gpu(12288))).toBe("m:8b-q8_0");
  });

  it("falls back to the smallest when nothing fits comfortably", () => {
    expect(recommendVariantTag(variants, "8b", gpu(2048))).toBe("m:8b-q4_K_M");
  });

  it("prefers a balanced q4 when there is no GPU signal", () => {
    expect(recommendVariantTag(variants, "8b", undefined)).toBe("m:8b-q4_K_M");
  });

  it("returns null for an empty list", () => {
    expect(recommendVariantTag([], "8b", undefined)).toBeNull();
  });

  it("judges fit by the real size when the core supplied one (#571)", () => {
    // The estimate says an 8B q8 (~8.1 GB ×1.2) fits 12 GB — but the real size (11 GB)
    // doesn't; the recommendation must believe the tags page, not the estimate.
    const real = [v("m:8b-q4_K_M", "q4_K_M", 4.9), v("m:8b-q8_0", "q8_0", 11.0)];
    expect(recommendVariantTag(real, "8b", gpu(12288))).toBe("m:8b-q4_K_M");
  });

  it("never recommends a cloud alias", () => {
    const withCloud = [v("m:480b-cloud", ""), v("m:8b-q4_K_M", "q4_K_M")];
    expect(recommendVariantTag(withCloud, "8b", undefined)).toBe("m:8b-q4_K_M");
    expect(recommendVariantTag([v("m:cloud", "")], null, undefined)).toBeNull();
  });
});
