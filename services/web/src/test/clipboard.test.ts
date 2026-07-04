import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText } from "@/lib/clipboard";

// copyText prefers the async clipboard API and falls back to the legacy selection path —
// a plain-HTTP LAN origin (#460) has no navigator.clipboard at all.

const setClipboard = (value: unknown) =>
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });

afterEach(() => {
  setClipboard(undefined);
  delete (document as { execCommand?: unknown }).execCommand;
});

describe("copyText", () => {
  it("uses the async clipboard API when present", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });
    await expect(copyText("hello")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("falls back to execCommand when the API is absent (insecure origin)", async () => {
    setClipboard(undefined);
    const exec = vi.fn().mockReturnValue(true);
    (document as { execCommand?: unknown }).execCommand = exec;
    await expect(copyText("hello")).resolves.toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
  });

  it("falls back when the API refuses (blocked permission / unfocused document)", async () => {
    setClipboard({ writeText: vi.fn().mockRejectedValue(new Error("NotAllowed")) });
    const exec = vi.fn().mockReturnValue(true);
    (document as { execCommand?: unknown }).execCommand = exec;
    await expect(copyText("hello")).resolves.toBe(true);
  });

  it("reports failure instead of throwing when every path is unavailable", async () => {
    setClipboard(undefined);
    await expect(copyText("hello")).resolves.toBe(false);
  });

  it("cleans its scratch textarea out of the DOM", async () => {
    setClipboard(undefined);
    (document as { execCommand?: unknown }).execCommand = vi.fn().mockReturnValue(true);
    await copyText("hello");
    expect(document.querySelector("textarea")).toBeNull();
  });
});
