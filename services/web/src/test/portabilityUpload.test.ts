/** The archive upload — its error shapes (#887) and its progress (#893).
 *
 *  Two things this one request must do that no other request in the app does.
 *
 *  It can plausibly be answered by something *other* than the core: a proxy in front of it
 *  refusing a multi-gigabyte body on size. The core always answers JSON with a `detail`;
 *  anything else is not the core, and must not be flattened into a bare status line —
 *  "Request Entity Too Large" over HTTP/1.1, and nothing at all over HTTP/2, where
 *  `statusText` is empty by spec.
 *
 *  And it must report how far the body has got, which is why it is the app's only
 *  `XMLHttpRequest`: `fetch` has no upload-progress event, so a ten-minute upload of a real
 *  archive is indistinguishable from a hung one. The taxonomy above had to survive that
 *  change intact — hence a fake XHR here rather than a stubbed `fetch`, and hence the
 *  connectivity evidence (#494/#791) being checked too, since leaving the shared `epFetch`
 *  behind is exactly how a request quietly stops reporting that the box has gone away.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, ProxyError } from "@/lib/api";
import { useConnection } from "@/stores/connection";

const archive = () => new File(["tenant archive"], "epicurus-local.tar.gz");

type Listener = (event: Partial<ProgressEvent>) => void;

/** The slice of XMLHttpRequest the upload uses, driven by hand.
 *
 *  Deliberately *not* a full XHR polyfill: the point is to control exactly when the body
 *  progresses and when the response lands, which a real implementation would decide for us. */
class FakeXhr {
  static last: FakeXhr | null = null;

  status = 200;
  responseText = "{}";
  method = "";
  url = "";
  sent: unknown = null;
  upload = { listeners: new Map<string, Listener[]>(), addEventListener: FakeXhr.add };
  listeners = new Map<string, Listener[]>();

  static add(this: { listeners: Map<string, Listener[]> }, type: string, fn: Listener): void {
    const bucket = this.listeners.get(type) ?? [];
    bucket.push(fn);
    this.listeners.set(type, bucket);
  }

  constructor() {
    FakeXhr.last = this;
  }

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
  }

  addEventListener(type: string, fn: Listener): void {
    FakeXhr.add.call(this, type, fn);
  }

  send(body: unknown): void {
    this.sent = body;
  }

  /** Fire an upload-progress event, as the browser would while the body drains. */
  progress(loaded: number, total: number, lengthComputable = true): void {
    for (const fn of this.upload.listeners.get("progress") ?? []) {
      fn({ loaded, total, lengthComputable });
    }
  }

  /** The body is away — the browser's `upload` load, distinct from the response's. */
  bodySent(): void {
    for (const fn of this.upload.listeners.get("load") ?? []) fn({});
  }

  /** The response lands with the given status and body. */
  respond(status: number, responseText: string): void {
    this.status = status;
    this.responseText = responseText;
    for (const fn of this.listeners.get("load") ?? []) fn({});
  }

  /** The connection died — no response at all. */
  networkError(): void {
    for (const fn of this.listeners.get("error") ?? []) fn({});
  }
}

const JOB = {
  id: "imp-1",
  status: "staged",
  created_at: "2026-09-06T09:00:00+00:00",
  updated_at: "2026-09-06T09:00:00+00:00",
  progress: [],
  preview: null,
  report: null,
  error: null,
};

beforeEach(() => {
  useConnection.setState({ coreDown: false, pendingDown: false, confirmedDown: false });
  FakeXhr.last = null;
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** Start the upload and hand back the promise plus the XHR it created. */
function start(onProgress?: (f: number | null) => void) {
  const promise = api.uploadPortabilityArchive(archive(), onProgress);
  const xhr = FakeXhr.last;
  if (xhr === null) throw new Error("no XMLHttpRequest was opened");
  return { promise, xhr };
}

describe("uploadPortabilityArchive error shapes (#887)", () => {
  it("posts to the exact path the nginx exemption matches", async () => {
    const { promise, xhr } = start();
    xhr.respond(400, JSON.stringify({ detail: "nope" }));

    await expect(promise).rejects.toBeInstanceOf(ApiError);
    expect(xhr.method).toBe("POST");
    // The proxy exemption is an *exact*-match location: no trailing slash, no query string.
    expect(xhr.url).toBe("/platform/v1/portability/imports");
  });

  it("keeps the core's own sentence when the core refuses", async () => {
    const { promise, xhr } = start();
    xhr.respond(413, JSON.stringify({ detail: "archive exceeds the 4096 MiB upload limit" }));

    const error = await promise.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(ProxyError);
    expect((error as ApiError).detail).toBe("archive exceeds the 4096 MiB upload limit");
  });

  it("marks a proxy's HTML 413 as a ProxyError, not a bare status line", async () => {
    const { promise, xhr } = start();
    xhr.respond(413, "<html><head><title>413 Request Entity Too Large</title></head></html>");

    const error = await promise.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProxyError);
    expect((error as ProxyError).status).toBe(413);
    expect((error as ProxyError).detail).toBe("HTTP 413");
  });

  it("marks an empty-bodied refusal too (HTTP/2 carries no status text)", async () => {
    const { promise, xhr } = start();
    xhr.respond(413, "");

    await expect(promise).rejects.toBeInstanceOf(ProxyError);
  });

  it("reports a connection that died mid-body the way a fetch failure did", async () => {
    const { promise, xhr } = start();
    xhr.networkError();

    // The card branches on this exact shape to name a proxy rather than print nothing.
    await expect(promise).rejects.toBeInstanceOf(TypeError);
  });
});

describe("uploadPortabilityArchive progress (#893)", () => {
  it("reports a fraction as the body drains, and completes when it is away", async () => {
    const seen: (number | null)[] = [];
    const { promise, xhr } = start((f) => seen.push(f));

    xhr.progress(25, 100);
    xhr.progress(90, 100);
    xhr.bodySent();
    xhr.respond(200, JSON.stringify(JOB));

    await expect(promise).resolves.toMatchObject({ id: "imp-1", status: "staged" });
    // The last value is 1 even though no progress event said so: the bar has to finish
    // before the core's read of a multi-gigabyte tar begins, or it parks at 90% for minutes.
    expect(seen).toEqual([0.25, 0.9, 1]);
  });

  it("says null — not 0% — when the browser will not commit to a total", async () => {
    const seen: (number | null)[] = [];
    const { promise, xhr } = start((f) => seen.push(f));

    xhr.progress(4096, 0, false);
    xhr.respond(200, JSON.stringify(JOB));

    await promise;
    // A percentage nothing measured would be a fabrication; the card says "Uploading…".
    expect(seen).toEqual([null]);
  });
});

describe("uploadPortabilityArchive connectivity evidence (#494, kept through #893)", () => {
  it("reports the core unreachable when the request never lands", async () => {
    const { promise, xhr } = start();
    xhr.networkError();
    await promise.catch(() => undefined);

    expect(useConnection.getState().coreDown).toBe(true);
  });

  it("treats a 413 from the core as proof it is up — an error is still an answer", async () => {
    useConnection.setState({ coreDown: true });
    const { promise, xhr } = start();
    xhr.respond(413, JSON.stringify({ detail: "too big" }));
    await promise.catch(() => undefined);

    expect(useConnection.getState().coreDown).toBe(false);
  });

  it("reports a gateway 502 as unreachable — nginx answered, the core did not", async () => {
    const { promise, xhr } = start();
    xhr.respond(502, "<html>502</html>");
    await promise.catch(() => undefined);

    expect(useConnection.getState().coreDown).toBe(true);
  });
});
