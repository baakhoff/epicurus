import { describe, expect, it } from "vitest";

import { GRANTED_ON_SERVER, decidePermission } from "../../src/main/permissions";

const SERVER = "https://epicurus.example.com";

describe("decidePermission", () => {
  it("grants clipboard writes and fullscreen to the server's own pages", () => {
    expect(decidePermission("clipboard-sanitized-write", `${SERVER}/chat`, SERVER)).toBe(true);
    expect(decidePermission("fullscreen", SERVER, SERVER)).toBe(true);
  });

  it.each([
    "notifications",
    "media",
    "geolocation",
    "clipboard-read",
    "midi",
    "midiSysex",
    "openExternal",
    "pointerLock",
    "display-capture",
    "hid",
    "serial",
    "usb",
    "idle-detection",
    "storage-access",
    "unknown",
  ])("denies %s even to the server", (permission) => {
    expect(decidePermission(permission, SERVER, SERVER)).toBe(false);
  });

  it("denies everything to any other origin", () => {
    for (const permission of GRANTED_ON_SERVER) {
      expect(decidePermission(permission, "https://id.example.com/login", SERVER)).toBe(false);
      expect(decidePermission(permission, "http://epicurus.example.com/", SERVER)).toBe(false);
      expect(decidePermission(permission, "epicurus-app://local/connect.html", SERVER)).toBe(false);
    }
  });

  it("denies everything when no server is saved, or the origin is unparsable or opaque", () => {
    expect(decidePermission("fullscreen", SERVER, null)).toBe(false);
    expect(decidePermission("fullscreen", "", SERVER)).toBe(false);
    expect(decidePermission("fullscreen", "data:text/html,x", SERVER)).toBe(false);
  });
});
