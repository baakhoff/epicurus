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
 */
import { useConnection } from "@/stores/connection";

export async function epFetch(
  input: string | URL | Request,
  init?: RequestInit,
): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(input, init);
  } catch (err) {
    if (err instanceof TypeError) useConnection.getState().reportUnreachable();
    throw err;
  }
  if (response.status === 502 || response.status === 504) {
    useConnection.getState().reportUnreachable();
  } else {
    useConnection.getState().reportReachable();
  }
  return response;
}
