/**
 * A minimal SSE reader over fetch — EventSource cannot POST a body, and both
 * the agent chat and model pulls stream from POST endpoints.
 */

export interface SseMessage {
  event: string;
  data: string;
}

/** Parse a complete SSE frame block (the text between blank lines). */
export function parseFrame(block: string): SseMessage | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // comment / heartbeat
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  return { event, data: data.join("\n") };
}

/**
 * POST `body` to `path` and yield each SSE message. Abort via `signal`.
 * Throws ApiError-shaped objects for non-OK responses (the stream never began).
 */
export async function* sse(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<SseMessage> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
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
