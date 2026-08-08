/**
 * The one fetch every /platform call goes through (#494). Each request the app already
 * makes doubles as connectivity evidence — there is no dedicated probe endpoint:
 *
 * - a network-level failure (fetch TypeError — no route, DNS, box off) → unreachable;
 * - a gateway 502/504 (nginx answered, the core container did not) → unreachable;
 * - ANY other response → reachable, errors included: a 404 or 500 proves epicurus
 *   answered, and 503 is deliberately NOT "down" — the LLM surface uses it for the
 *   *paused* state (PausedError), which is a mood, not an outage.
 *
 * Aborts (cancelled requests, unmounted queries) are no evidence either way. Long-lived
 * SSE bodies report only their connect; a mid-stream drop surfaces through the chat
 * re-attach loop (#477), whose `activeRun` probes land right back here.
 *
 * A failure report carries *which* request and *how* it failed (#791) — method, path,
 * and failure class — so a flap can be attributed from the banner's tooltip instead of
 * network-tab archaeology. Deciding whether that evidence is enough to actually trip the
 * banner is not this function's job: it stays pure evidence reporting, and the store
 * (`useConnection`) owns the debounce.
 */
import { useConnection, type UnreachableKind } from "@/stores/connection";

/** The path epicurus actually routes on — no origin, no query string (the latter can
 *  carry a search term or similar; keep it out of a diagnostic that lands in a tooltip
 *  and the console). Falls back to the raw input if it isn't a parseable URL. */
function requestPath(input: string | URL | Request): string {
  const raw = input instanceof Request ? input.url : input.toString();
  try {
    return new URL(raw, "http://epicurus.local").pathname;
  } catch {
    return raw;
  }
}

function requestMethod(input: string | URL | Request, init?: RequestInit): string {
  const method = init?.method ?? (input instanceof Request ? input.method : "GET");
  return method.toUpperCase();
}

export async function epFetch(
  input: string | URL | Request,
  init?: RequestInit,
): Promise<Response> {
  const evidence = (kind: UnreachableKind) => ({
    method: requestMethod(input, init),
    path: requestPath(input),
    kind,
  });
  let response: Response;
  try {
    response = await fetch(input, init);
  } catch (err) {
    if (err instanceof TypeError) useConnection.getState().reportUnreachable(evidence("TypeError"));
    throw err;
  }
  if (response.status === 502 || response.status === 504) {
    useConnection.getState().reportUnreachable(evidence(response.status === 502 ? "502" : "504"));
  } else {
    useConnection.getState().reportReachable();
  }
  return response;
}
