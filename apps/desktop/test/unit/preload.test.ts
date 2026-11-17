import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { CHANNELS } from "../../src/main/channels";

const preload = readFileSync(join(__dirname, "..", "..", "src", "preload", "preload.ts"), "utf8");

describe("the preload", () => {
  it("repeats exactly the main process's channel names", () => {
    const block = /const CHANNELS = \{([\s\S]*?)\} as const;/.exec(preload)?.[1] ?? "";
    const repeated = Object.fromEntries(
      [...block.matchAll(/(\w+): "([^"]+)"/g)].map((m) => [m[1], m[2]]),
    );
    expect(repeated).toEqual(CHANNELS);
  });

  it("imports nothing but electron (a sandboxed preload cannot)", () => {
    const imports = [...preload.matchAll(/^import .* from "([^"]+)";$/gm)].map((m) => m[1]);
    expect(imports).toEqual(["electron"]);
  });

  it("exposes the shell API only on the app's own pages, and no ipcRenderer", () => {
    expect(preload).toContain('if (location.protocol === "epicurus-app:")');
    expect(preload).not.toMatch(/exposeInMainWorld\([^)]*ipcRenderer\s*\)/);
    expect(preload).toContain("webPush: false");
  });
});
