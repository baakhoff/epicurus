import { describe, expect, it } from "vitest";

import { recommendKvCache } from "@/lib/kvCacheFit";
import type { SystemInfo } from "@/lib/contracts";

// A system with the given VRAM (MB); pass null for a GPU-less host.
function sys(vramMb: number | null): SystemInfo {
  if (vramMb == null) return { ram_total_mb: 32000 };
  return {
    gpu: { vendor: "nvidia", name: "GPU", vram_total_mb: vramMb, vram_free_mb: vramMb },
    ram_total_mb: 32000,
  };
}

describe("recommendKvCache", () => {
  it("returns null when there is no system info to judge against", () => {
    expect(recommendKvCache(undefined)).toBeNull();
  });

  it("keeps the full-precision cache on ample VRAM (≥16 GB)", () => {
    expect(recommendKvCache(sys(24576))?.value).toBe("");
  });

  it("suggests q8_0 on moderate VRAM (8–16 GB)", () => {
    expect(recommendKvCache(sys(12288))?.value).toBe("q8_0");
  });

  it("suggests q4_0 on tight VRAM (<8 GB)", () => {
    expect(recommendKvCache(sys(6144))?.value).toBe("q4_0");
  });

  it("keeps f16 on a GPU-less host and explains why", () => {
    const rec = recommendKvCache(sys(null));
    expect(rec?.value).toBe("");
    expect(rec?.reason).toMatch(/no gpu/i);
  });

  it("treats the thresholds inclusively (16 GB → f16, 8 GB → q8_0)", () => {
    expect(recommendKvCache(sys(16384))?.value).toBe("");
    expect(recommendKvCache(sys(8192))?.value).toBe("q8_0");
  });
});
