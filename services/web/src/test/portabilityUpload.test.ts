/** The archive upload's error shapes (#887).
 *
 *  The upload is the one request that can plausibly be answered by something *other* than
 *  the core: a proxy in front of it refusing a multi-gigabyte body on size. The core always
 *  answers JSON with a `detail`; anything else is not the core, and must not be flattened
 *  into a bare status line — "Request Entity Too Large" over HTTP/1.1, and nothing at all
 *  over HTTP/2, where `statusText` is empty by spec.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, ProxyError } from "@/lib/api";
import { useConnection } from "@/stores/connection";

const archive = () => new File(["tenant archive"], "epicurus-local.tar.gz");

function stubFetch(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  useConnection.setState({ coreDown: false, pendingDown: false, confirmedDown: false });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("uploadPortabilityArchive error shapes (#887)", () => {
  it("posts to the exact path the nginx exemption matches", async () => {
    const fetchMock = stubFetch(
      new Response(JSON.stringify({ detail: "nope" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(api.uploadPortabilityArchive(archive())).rejects.toBeInstanceOf(ApiError);
    // The proxy exemption is an *exact*-match location: no trailing slash, no query string.
    expect(fetchMock.mock.calls[0][0]).toBe("/platform/v1/portability/imports");
  });

  it("keeps the core's own sentence when the core refuses", async () => {
    stubFetch(
      new Response(JSON.stringify({ detail: "archive exceeds the 4096 MiB upload limit" }), {
        status: 413,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const error = await api.uploadPortabilityArchive(archive()).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(ProxyError);
    expect((error as ApiError).detail).toBe("archive exceeds the 4096 MiB upload limit");
  });

  it("marks a proxy's HTML 413 as a ProxyError, not a bare status line", async () => {
    stubFetch(
      new Response("<html><head><title>413 Request Entity Too Large</title></head></html>", {
        status: 413,
        statusText: "Request Entity Too Large",
        headers: { "Content-Type": "text/html" },
      }),
    );

    const error = await api.uploadPortabilityArchive(archive()).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProxyError);
    expect((error as ProxyError).status).toBe(413);
    expect((error as ProxyError).detail).toBe("HTTP 413");
  });

  it("marks an empty-bodied refusal too (HTTP/2 carries no status text)", async () => {
    stubFetch(new Response(null, { status: 413 }));

    await expect(api.uploadPortabilityArchive(archive())).rejects.toBeInstanceOf(ProxyError);
  });
});
