import { describe, expect, it } from "vitest";

import {
  isLoopbackHost,
  needsInsecureConfirmation,
  normaliseServerAddress,
  type NormalisedAddress,
} from "../../src/main/address";

function ok(input: string): NormalisedAddress {
  const result = normaliseServerAddress(input);
  if (!result.ok) throw new Error(`expected ${input} to normalise, got: ${result.error}`);
  return result;
}

function error(input: string): string {
  const result = normaliseServerAddress(input);
  if (result.ok) throw new Error(`expected ${input} to be refused, got ${result.url}`);
  return result.error;
}

describe("normaliseServerAddress", () => {
  it("reduces a full URL to its origin with a trailing slash", () => {
    expect(ok("https://epicurus.example.com/settings?tab=a#x").url).toBe(
      "https://epicurus.example.com/",
    );
  });

  it("assumes https for a bare host name", () => {
    const address = ok("epicurus.example.com");
    expect(address.url).toBe("https://epicurus.example.com/");
    expect(address.insecure).toBe(false);
    expect(address.loopback).toBe(false);
  });

  it("assumes http for a bare loopback host, keeping the port", () => {
    expect(ok("localhost:8080").url).toBe("http://localhost:8080/");
    expect(ok("127.0.0.1:3000/chat").url).toBe("http://127.0.0.1:3000/");
    expect(ok("[::1]:8080").url).toBe("http://[::1]:8080/");
    expect(ok("epicurus.localhost").url).toBe("http://epicurus.localhost/");
  });

  it("assumes https for a bare LAN address — only loopback gets plain http by default", () => {
    expect(ok("192.168.1.10:8080").url).toBe("https://192.168.1.10:8080/");
  });

  it("keeps an explicit http:// and flags it as insecure", () => {
    const address = ok("http://nas.lan:8080");
    expect(address).toMatchObject({
      url: "http://nas.lan:8080/",
      origin: "http://nas.lan:8080",
      host: "nas.lan:8080",
      insecure: true,
      loopback: false,
    });
  });

  it("drops a default port and lower-cases the host", () => {
    expect(ok("HTTPS://Epicurus.Example.COM:443/").url).toBe("https://epicurus.example.com/");
    expect(ok("http://Example.com:80").url).toBe("http://example.com/");
  });

  it("ignores surrounding whitespace", () => {
    expect(ok("  https://a.example \n").url).toBe("https://a.example/");
  });

  it("converts an internationalised host name to its ASCII form", () => {
    expect(ok("https://bücher.example").host).toBe("xn--bcher-kva.example");
  });

  it.each([
    ["", "Enter the address"],
    ["   ", "Enter the address"],
    ["https://a b.example", "cannot contain spaces"],
    ["ftp://files.example", "Only http:// and https://"],
    ["file:///etc/passwd", "Only http:// and https://"],
    ["javascript:alert(1)", "Only http:// and https://"],
    ["mailto:me@example.com", "Only http:// and https://"],
    ["https://user:secret@example.com", "user name and password"],
    ["https://user@example.com", "user name and password"],
    ["https://", "does not look like a web address"],
    ["http://[not-an-ip]", "does not look like a web address"],
  ])("refuses %j", (input, message) => {
    expect(error(input)).toContain(message);
  });
});

describe("isLoopbackHost", () => {
  it.each(["localhost", "LOCALHOST", "localhost.", "app.localhost", "127.0.0.1", "127.255.0.9", "[::1]"])(
    "%s is loopback",
    (host) => expect(isLoopbackHost(host)).toBe(true),
  );
  it.each(["example.com", "10.0.0.1", "192.168.1.1", "127.0.0.1.example.com", "128.0.0.1", "localhost.example.com", "[::2]"])(
    "%s is not loopback",
    (host) => expect(isLoopbackHost(host)).toBe(false),
  );
});

describe("needsInsecureConfirmation", () => {
  it("is required only for plain http to a host that is not this machine", () => {
    expect(needsInsecureConfirmation(ok("http://nas.lan"))).toBe(true);
    expect(needsInsecureConfirmation(ok("http://192.168.1.4:8080"))).toBe(true);
    expect(needsInsecureConfirmation(ok("http://localhost:8080"))).toBe(false);
    expect(needsInsecureConfirmation(ok("https://nas.lan"))).toBe(false);
  });
});
