/**
 * Is this address an epicurus server? — asked of `GET /platform/v1/info`.
 *
 * Runs in the main process (through the window's own Chromium network stack, so proxies and the
 * operating system's certificate store apply exactly as they will for the window) and answers
 * with one of four outcomes the "Connect to a server" page can explain:
 *
 * - `ok` — the server answered `/info` with the platform's shape (its `core_app_version` is shown).
 * - `ok` with `signInRequired` — sign-in is on (`AUTH_MODE=oidc`), so `/info` answers 401 to a
 *   browser without a session. `GET /platform/v1/auth/session` is exempt from sign-in, so it
 *   confirms it is an epicurus server.
 * - `not-epicurus` — something answered, but not like an epicurus server.
 * - `unreachable` / `certificate` — nothing answered, or the TLS certificate was refused.
 */

export const INFO_PATH = "/platform/v1/info";
export const SESSION_PATH = "/platform/v1/auth/session";
export const CHECK_TIMEOUT_MS = 10_000;

export type ServerInfo = {
  version: string | null;
  releaseTrack: string | null;
  signInRequired: boolean;
};

export type CheckFailureKind = "unreachable" | "certificate" | "not-epicurus" | "timeout";

export type CheckResult =
  | { ok: true; info: ServerInfo }
  | { ok: false; kind: CheckFailureKind; message: string };

export type FetchLike = (
  url: string,
  init: { signal: AbortSignal; headers: Record<string, string>; redirect: "manual" },
) => Promise<FetchResponse>;

export type FetchResponse = {
  status: number;
  headers: { get(name: string): string | null };
  json(): Promise<unknown>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `true` when `body` has the fields `PlatformInfo` always carries. */
export function looksLikePlatformInfo(body: unknown): body is Record<string, unknown> {
  return (
    isRecord(body) &&
    typeof body.contract_version === "string" &&
    typeof body.core_version === "string" &&
    typeof body.tenant === "string"
  );
}

/** `true` when `body` is the `/auth/session` shape. */
export function looksLikeAuthSession(body: unknown): boolean {
  return isRecord(body) && typeof body.mode === "string" && typeof body.signed_in === "boolean";
}

/** A Chromium network error (`net::ERR_…`) or any thrown error, as a failure the user can act on. */
export function describeNetworkError(error: unknown, host: string): CheckResult & { ok: false } {
  const text = error instanceof Error ? `${error.name} ${error.message}` : String(error);
  if (/AbortError|TimeoutError|ERR_TIMED_OUT|ERR_CONNECTION_TIMED_OUT/.test(text)) {
    return { ok: false, kind: "timeout", message: `${host} did not answer in time.` };
  }
  if (/ERR_CERT_|ERR_SSL_|ERR_BAD_SSL|certificate/i.test(text)) {
    return {
      ok: false,
      kind: "certificate",
      message:
        `${host} presented a certificate this computer does not trust. ` +
        "Use an address with a valid certificate, or add your certificate authority to the " +
        "operating system's trust store.",
    };
  }
  if (/ERR_NAME_NOT_RESOLVED|ENOTFOUND|EAI_AGAIN/.test(text)) {
    return { ok: false, kind: "unreachable", message: `No server named ${host} could be found.` };
  }
  return {
    ok: false,
    kind: "unreachable",
    message: `Could not reach ${host}. Check the address and that the server is running.`,
  };
}

const notEpicurus = (host: string, detail: string): CheckResult => ({
  ok: false,
  kind: "not-epicurus",
  message: `${host} answered, but it is not an epicurus server (${detail}).`,
});

async function readJson(response: { json(): Promise<unknown> }): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/** The server sends its API somewhere else — usually `http:` → `https:`. Say where. */
export function describeRedirect(origin: string, location: string | null): CheckResult {
  const host = new URL(origin).host;
  let target: URL | null;
  try {
    target = location ? new URL(location, origin) : null;
  } catch {
    target = null;
  }
  if (target && (target.protocol === "https:" || target.protocol === "http:")) {
    return {
      ok: false,
      kind: "not-epicurus",
      message: `${host} redirects to ${target.origin}. Connect to ${target.origin} instead.`,
    };
  }
  return notEpicurus(host, "it redirects /platform/v1/info");
}

/** Check the server at `origin` (`scheme://host[:port]`). Never throws. */
export async function checkServer(
  origin: string,
  fetchImpl: FetchLike,
  timeoutMs: number = CHECK_TIMEOUT_MS,
): Promise<CheckResult> {
  const host = new URL(origin).host;
  const get = async (path: string) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetchImpl(`${origin}${path}`, {
        signal: controller.signal,
        headers: { accept: "application/json" },
        redirect: "manual",
      });
    } finally {
      clearTimeout(timer);
    }
  };

  let response: FetchResponse;
  try {
    response = await get(INFO_PATH);
  } catch (error) {
    return describeNetworkError(error, host);
  }

  if (response.status === 200) {
    const body = await readJson(response);
    if (!looksLikePlatformInfo(body)) return notEpicurus(host, "unexpected answer from /platform/v1/info");
    const version =
      typeof body.core_app_version === "string" ? body.core_app_version : body.core_version;
    return {
      ok: true,
      info: {
        version: typeof version === "string" ? version : null,
        releaseTrack: typeof body.release_track === "string" ? body.release_track : null,
        signInRequired: false,
      },
    };
  }

  if (response.status === 401) {
    const body = await readJson(response);
    if (!isRecord(body) || body.code !== "unauthenticated") {
      return notEpicurus(host, "HTTP 401 without the platform's sign-in answer");
    }
    let session: FetchResponse;
    try {
      session = await get(SESSION_PATH);
    } catch (error) {
      return describeNetworkError(error, host);
    }
    if (session.status !== 200 || !looksLikeAuthSession(await readJson(session))) {
      return notEpicurus(host, "no sign-in endpoint");
    }
    return { ok: true, info: { version: null, releaseTrack: null, signInRequired: true } };
  }

  if (response.status >= 300 && response.status < 400) {
    return describeRedirect(origin, response.headers.get("location"));
  }
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    return {
      ok: false,
      kind: "unreachable",
      message: `${host} answered HTTP ${response.status}: the server behind it is not running.`,
    };
  }
  return notEpicurus(host, `HTTP ${response.status} from /platform/v1/info`);
}

/**
 * A failed page load in the window (`did-fail-load`'s `errorDescription`, e.g.
 * `ERR_CONNECTION_REFUSED`) as the message the offline page shows. Codes -200 to -299 are
 * certificate errors; the rest of the connection range reads as "not reachable".
 */
export function describeLoadFailure(
  errorCode: number,
  errorDescription: string,
  host: string,
): { kind: CheckFailureKind; message: string } {
  const text = errorCode <= -200 && errorCode > -300 ? `ERR_CERT_ ${errorDescription}` : errorDescription;
  const { kind, message } = describeNetworkError(new Error(text), host);
  return { kind, message };
}
