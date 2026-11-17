import { mkdirSync, mkdtempSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { mustRefuseRemoteDebugging } from "../../src/main/debugging";

const switches =
  (...present: string[]) =>
  (name: string) =>
    present.includes(name);

describe("mustRefuseRemoteDebugging", () => {
  const root = mkdtempSync(join(tmpdir(), "epicurus-debugging-"));
  const profile = join(root, "Epicurus");
  mkdirSync(profile);
  const other = join(root, "smoke");
  const link = join(root, "link-to-profile");
  symlinkSync(profile, link);

  it("lets a normal start through", () => {
    expect(mustRefuseRemoteDebugging(switches(), undefined, profile)).toBe(false);
    expect(mustRefuseRemoteDebugging(switches("lang"), undefined, profile)).toBe(false);
  });

  it.each(["remote-debugging-port", "remote-debugging-pipe"])(
    "refuses %s on the default profile",
    (name) => {
      expect(mustRefuseRemoteDebugging(switches(name), undefined, profile)).toBe(true);
      expect(mustRefuseRemoteDebugging(switches(name), "", profile)).toBe(true);
    },
  );

  it("refuses an override that is the default profile under another spelling", () => {
    const port = switches("remote-debugging-port");
    expect(mustRefuseRemoteDebugging(port, `${profile}/`, profile, "linux")).toBe(true);
    expect(mustRefuseRemoteDebugging(port, join(profile, "..", "Epicurus"), profile)).toBe(true);
    expect(mustRefuseRemoteDebugging(port, link, profile, "linux")).toBe(true);
    expect(mustRefuseRemoteDebugging(port, profile.toUpperCase(), profile, "darwin")).toBe(true);
    expect(mustRefuseRemoteDebugging(port, profile.toUpperCase(), profile, "win32")).toBe(true);
  });

  it("allows debugging on a separate profile, as the launch smoke runs", () => {
    const port = switches("remote-debugging-port");
    expect(mustRefuseRemoteDebugging(port, other, profile, "linux")).toBe(false);
    expect(mustRefuseRemoteDebugging(port, root, profile, "linux")).toBe(false);
  });
});
