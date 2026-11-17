import { describe, expect, it, vi } from "vitest";

import {
  INFO_PATH,
  SESSION_PATH,
  checkServer,
  describeLoadFailure,
  describeNetworkError,
  type FetchLike,
  type FetchResponse,
} from "../../src/main/serverCheck";

const ORIGIN = "https://epicurus.example.com";

const INFO = {
  contract_version: "1.0",
  core_version: "0.42.0",
  core_app_version: "0.131.0",
  library_version: "0.42.0",
  release_track: "latest",
  tenant: "local",
};

function response(status: number, body?: unknown, headers: Record<string, string> = {}): FetchResponse {
  return {
    status,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    json: async () => {
      if (body === undefined) throw new SyntaxError("Unexpected token <");
      return body;
    },
  };
}

function routes(table: Record<string, FetchResponse | Error>): FetchLike & ReturnType<typeof vi.fn> {
  return vi.fn(async (url: string) => {
    const path = new URL(url).pathname;
    const answer = table[path];
    if (answer === undefined) return response(404, { detail: "Not Found" });
    if (answer instanceof Error) throw answer;
    return answer;
  });
}

describe("checkServer", () => {
  it("accepts a server whose /info has the platform's shape, and reports its core-app version", async () => {
    const fetch = routes({ [INFO_PATH]: response(200, INFO) });
    expect(await checkServer(ORIGIN, fetch)).toEqual({
      ok: true,
      info: { version: "0.131.0", releaseTrack: "latest", signInRequired: false },
    });
    expect(fetch).toHaveBeenCalledWith(`${ORIGIN}${INFO_PATH}`, expect.objectContaining({ redirect: "manual" }));
  });

  it("falls back to core_version for a core older than core_app_version", async () => {
    const { core_app_version: _drop, release_track: _track, ...old } = INFO;
    const result = await checkServer(ORIGIN, routes({ [INFO_PATH]: response(200, old) }));
    expect(result).toEqual({ ok: true, info: { version: "0.42.0", releaseTrack: null, signInRequired: false } });
  });

  it("accepts a server with sign-in on: /info answers 401, /auth/session confirms", async () => {
    const fetch = routes({
      [INFO_PATH]: response(401, { detail: "Sign in to continue.", code: "unauthenticated" }),
      [SESSION_PATH]: response(200, { mode: "oidc", signed_in: false, provider_name: "Pocket ID" }),
    });
    expect(await checkServer(ORIGIN, fetch)).toEqual({
      ok: true,
      info: { version: null, releaseTrack: null, signInRequired: true },
    });
  });

  it("refuses a 401 that is not the platform's sign-in answer", async () => {
    const result = await checkServer(ORIGIN, routes({ [INFO_PATH]: response(401, { error: "nope" }) }));
    expect(result).toMatchObject({ ok: false, kind: "not-epicurus" });
  });

  it("refuses a 401 whose /auth/session is not the platform's", async () => {
    const result = await checkServer(
      ORIGIN,
      routes({
        [INFO_PATH]: response(401, { code: "unauthenticated" }),
        [SESSION_PATH]: response(200, { hello: "world" }),
      }),
    );
    expect(result).toMatchObject({ ok: false, kind: "not-epicurus" });
  });

  it("refuses a web server that answers /info with HTML", async () => {
    const result = await checkServer(ORIGIN, routes({ [INFO_PATH]: response(200) }));
    expect(result).toMatchObject({ ok: false, kind: "not-epicurus" });
    if (!result.ok) expect(result.message).toContain("not an epicurus server");
  });

  it("refuses JSON that is not PlatformInfo", async () => {
    const result = await checkServer(ORIGIN, routes({ [INFO_PATH]: response(200, { status: "ok" }) }));
    expect(result).toMatchObject({ ok: false, kind: "not-epicurus" });
  });

  it("refuses a 404", async () => {
    const result = await checkServer(ORIGIN, routes({}));
    expect(result).toMatchObject({ ok: false, kind: "not-epicurus" });
    if (!result.ok) expect(result.message).toContain("HTTP 404");
  });

  it("names the origin a redirect points to", async () => {
    const result = await checkServer(
      "http://epicurus.example.com",
      routes({ [INFO_PATH]: response(301, undefined, { location: "https://epicurus.example.com/platform/v1/info" }) }),
    );
    expect(result).toMatchObject({ ok: false, kind: "not-epicurus" });
    if (!result.ok) expect(result.message).toContain("Connect to https://epicurus.example.com instead");
  });

  it("reads a 502/503/504 as the server behind a proxy being down", async () => {
    for (const status of [502, 503, 504]) {
      const result = await checkServer(ORIGIN, routes({ [INFO_PATH]: response(status) }));
      expect(result).toMatchObject({ ok: false, kind: "unreachable" });
    }
  });

  it.each([
    ["net::ERR_CONNECTION_REFUSED", "unreachable", "Could not reach"],
    ["net::ERR_NAME_NOT_RESOLVED", "unreachable", "No server named"],
    ["net::ERR_CERT_AUTHORITY_INVALID", "certificate", "certificate"],
    ["net::ERR_CERT_COMMON_NAME_INVALID", "certificate", "certificate"],
    ["net::ERR_SSL_PROTOCOL_ERROR", "certificate", "certificate"],
    ["net::ERR_CONNECTION_TIMED_OUT", "timeout", "did not answer in time"],
  ])("maps a thrown %s to %s", async (message, kind, text) => {
    const result = await checkServer(ORIGIN, routes({ [INFO_PATH]: new TypeError(message) }));
    expect(result).toMatchObject({ ok: false, kind });
    if (!result.ok) expect(result.message).toContain(text);
  });

  it("gives up after the timeout, aborting the request", async () => {
    let aborted = false;
    const hanging: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener("abort", () => {
          aborted = true;
          const error = new Error("This operation was aborted");
          error.name = "AbortError";
          reject(error);
        });
      });
    const result = await checkServer(ORIGIN, hanging, 20);
    expect(aborted).toBe(true);
    expect(result).toMatchObject({ ok: false, kind: "timeout" });
  });
});

describe("describeNetworkError and describeLoadFailure", () => {
  it("treats an unknown error as unreachable", () => {
    expect(describeNetworkError("boom", "h")).toMatchObject({ kind: "unreachable" });
  });

  it("maps Chromium's certificate range of load failures to a certificate message", () => {
    expect(describeLoadFailure(-202, "ERR_CERT_AUTHORITY_INVALID", "h").kind).toBe("certificate");
    expect(describeLoadFailure(-213, "SOMETHING_NEW", "h").kind).toBe("certificate");
    expect(describeLoadFailure(-102, "ERR_CONNECTION_REFUSED", "h").kind).toBe("unreachable");
    expect(describeLoadFailure(-118, "ERR_CONNECTION_TIMED_OUT", "h").kind).toBe("timeout");
  });
});
