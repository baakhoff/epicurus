import { describe, expect, it } from "vitest";

import type { SystemInfo } from "@/lib/contracts";
import { assessFit, estimateSizeMb, fitFilterOf, type ModelFit } from "@/lib/modelFit";

const GB = 1024;
const gpu = (vramGb: number, ramGb = 32): SystemInfo => ({
  gpu: { vendor: "nvidia", name: "Test GPU", vram_total_mb: vramGb * GB },
  ram_total_mb: ramGb * GB,
});
const cpuOnly = (ramGb: number): SystemInfo => ({ gpu: null, ram_total_mb: ramGb * GB });

describe("estimateSizeMb", () => {
  it("estimates from a params label (~0.6 GB/B at Q4)", () => {
    expect(estimateSizeMb("7b")).toBe(Math.round(7 * 0.6 * GB));
    expect(estimateSizeMb("3.8b")).toBe(Math.round(3.8 * 0.6 * GB));
  });
  it("multiplies MoE labels like 8x7b", () => {
    expect(estimateSizeMb("8x7b")).toBe(Math.round(56 * 0.6 * GB));
  });
  it("returns null when there's no parseable size", () => {
    expect(estimateSizeMb(null)).toBeNull();
    expect(estimateSizeMb("")).toBeNull();
    expect(estimateSizeMb("embedding")).toBeNull();
  });
});

describe("assessFit", () => {
  it("is unknown without a system or a size", () => {
    expect(assessFit(undefined, 4000).rating).toBe("unknown");
    expect(assessFit(gpu(24), null, null).rating).toBe("unknown");
  });

  it("rates a small model on a big GPU as a good fit", () => {
    const fit = assessFit(gpu(24), 4.3 * GB);
    expect(fit.rating).toBe("good");
    expect(fit.tone).toBe("ok");
    expect(fit.reason).toMatch(/VRAM/);
  });

  it("rates a model that just fits VRAM as tight", () => {
    const fit = assessFit(gpu(8), 7.8 * GB); // ~13B, just under 8 GB
    expect(fit.rating).toBe("tight");
    expect(fit.tone).toBe("warn");
  });

  it("flags a model bigger than VRAM but within RAM as partly on CPU", () => {
    const fit = assessFit(gpu(8, 32), 18 * GB); // ~30B
    expect(fit.rating).toBe("offload");
  });

  it("flags a model bigger than VRAM and RAM as too big", () => {
    const fit = assessFit(gpu(8, 16), 43 * GB); // ~70B
    expect(fit.rating).toBe("too_big");
    expect(fit.tone).toBe("danger");
  });

  it("rates a small model on a CPU-only box as cpu", () => {
    const fit = assessFit(cpuOnly(16), 1.8 * GB); // ~3B
    expect(fit.rating).toBe("cpu");
    expect(fit.reason).toMatch(/No GPU/);
  });

  it("estimates size from params for a catalog entry without a size", () => {
    const fit = assessFit(gpu(24), null, "7b");
    expect(fit.rating).toBe("good");
  });
});

describe("fitFilterOf", () => {
  const fit = (tone: ModelFit["tone"]): ModelFit => ({
    rating: "good",
    label: "x",
    reason: "",
    tone,
  });

  it("maps each judged tone to its filter bucket", () => {
    expect(fitFilterOf(fit("ok"))).toBe("ok");
    expect(fitFilterOf(fit("warn"))).toBe("warn");
    expect(fitFilterOf(fit("danger"))).toBe("danger");
  });

  it("returns null for an unjudgeable (dim) verdict — it matches no fit filter", () => {
    expect(fitFilterOf(fit("dim"))).toBeNull();
  });

  it("buckets real assessFit verdicts", () => {
    expect(fitFilterOf(assessFit(gpu(24), 4.3 * GB))).toBe("ok"); // small model, big GPU
    expect(fitFilterOf(assessFit(gpu(8, 16), 43 * GB))).toBe("danger"); // 70B won't fit
    expect(fitFilterOf(assessFit(undefined, 4000))).toBeNull(); // no system to judge
  });
});
