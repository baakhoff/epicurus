/**
 * A minimal SSE reader over fetch — EventSource cannot POST a body, and the agent
 * chat / model pulls stream from POST endpoints. The GET form is used to *re-attach*
 * to an in-flight turn after a disconnect (#376).
 */

export interface SseMessage {
  event: string;
  data: string;
  /** The frame's `id:` (a live-run sequence) when present — used to re-attach (#376). */
  id?: string;
}

/** Parse a complete SSE frame block (the text between blank lines). */
export function parseFrame(block: string): SseMessage | null {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // comment / heartbeat
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  return { event, data: data.join("\n"), id };
}

export interface SseInit {
  method?: "GET" | "POST";
  /** JSON body (POST only). */
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Open an SSE stream at `path` and yield each message. Abort via `signal`.
 * Throws ApiError-shaped objects for non-OK responses (the stream never began).
 */
export async function* sseRequest(path: string, init: SseInit = {}): AsyncGenerator<SseMessage> {
  const method = init.method ?? "POST";
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (method === "POST") headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: method === "POST" ? JSON.stringify(init.body) : undefined,
    signal: init.signal,
  });
  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw Object.assign(new Error(detail), { status: response.status });
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Frames are separated by a blank line; tolerate \r\n.
      let index;
      while ((index = buffer.search(/\n\n|\r\n\r\n/)) >= 0) {
        const block = buffer.slice(0, index).replace(/\r\n/g, "\n");
        buffer = buffer.slice(index + (buffer[index] === "\r" ? 4 : 2));
        const message = parseFrame(block);
        if (message) yield message;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** POST `body` to `path` and yield each SSE message (the common case). */
export function sse(path: string, body: unknown, signal?: AbortSignal): AsyncGenerator<SseMessage> {
  return sseRequest(path, { method: "POST", body, signal });
}
