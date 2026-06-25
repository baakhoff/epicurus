import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FitBadge } from "@/screens/ModelsScreen";
import type { SystemInfo } from "@/lib/contracts";

// A detected system with the given VRAM / RAM (megabytes).
function system(vramMb: number, ramMb: number): SystemInfo {
  return {
    gpu: { vendor: "nvidia", name: "GPU", vram_total_mb: vramMb, vram_free_mb: vramMb },
    ram_total_mb: ramMb,
  };
}

describe("FitBadge", () => {
  it("shows a check (ok tone) when the model fits the GPU with headroom", () => {
    render(<FitBadge system={system(24000, 32000)} sizeMb={4000} />);
    const badge = screen.getByRole("img", { name: /suitability: runs on gpu/i });
    expect(badge).toHaveClass("text-ok");
    // The full verdict + reason stays available on hover/tap (touch-friendly, no text chip).
    expect(badge).toHaveAttribute("title", expect.stringContaining("Runs on GPU"));
    expect(badge.querySelector("svg")).toBeInTheDocument();
  });

  it("shows a warning (warn tone) when the model is tight on VRAM", () => {
    render(<FitBadge system={system(24000, 32000)} sizeMb={22000} />);
    expect(screen.getByRole("img", { name: /suitability: tight on gpu/i })).toHaveClass("text-warn");
  });

  it("shows an X (danger tone) when the model is too big", () => {
    render(<FitBadge system={system(8000, 8000)} sizeMb={30000} />);
    expect(screen.getByRole("img", { name: /suitability: too big/i })).toHaveClass("text-danger");
  });

  it("renders nothing when there is no verdict (no system info)", () => {
    const { container } = render(<FitBadge system={undefined} sizeMb={4000} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("estimates from a params label when no concrete size is given", () => {
    // ~1B params ≈ 0.6 GB — trivially fits, so the catalog row still gets a verdict.
    render(<FitBadge system={system(24000, 32000)} sizeMb={null} params="1b" />);
    expect(screen.getByRole("img", { name: /suitability: runs on gpu/i })).toBeInTheDocument();
  });
});
