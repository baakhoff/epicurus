/**
 * A stand-in epicurus server for the launch smoke: a Node HTTP server that answers
 * `GET /platform/v1/info` like the core and serves a page the smoke can assert on.
 *
 * With `webDist` (a built `services/web/dist`) it serves the real web UI instead, answering the
 * handful of `/platform/` calls the shell makes on start-up with an empty, signed-out-free
 * single-user server, and 404 for the rest — enough for the screenshot to show the real UI.
 */
import { readFileSync, statSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { extname, join, normalize, resolve, sep } from "node:path";

export type Fixture = {
  origin: string;
  port: number;
  /** Requests seen, as `METHOD path`. */
  requests: string[];
  close(): Promise<void>;
};

export type FixtureOptions = {
  /** `"epicurus"` (default): a fixture page; `"other"`: a server that is not epicurus;
   *  `"web"`: the real built web UI from `webDist`. */
  kind?: "epicurus" | "other" | "web";
  webDist?: string;
  /** The origin a link on the fixture page points at, to leave the server in-window. */
  elsewhere?: string;
  port?: number;
};

export const INFO = {
  contract_version: "1.0",
  core_version: "0.42.0",
  core_app_version: "0.131.0",
  library_version: "0.42.0",
  release_track: "fixture",
  tenant: "local",
};

const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".woff2": "font/woff2",
};

function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

function fixturePage(req: IncomingMessage, res: ServerResponse, elsewhere: string): void {
  const restored = /(?:^|;\s*)fixture_session=1/.test(req.headers.cookie ?? "");
  const headers: Record<string, string> = { "content-type": "text/html; charset=utf-8" };
  if (!restored) headers["set-cookie"] = "fixture_session=1; Path=/; Max-Age=86400; SameSite=Lax";
  res.writeHead(200, headers);
  res.end(`<!doctype html>
<html><head><meta charset="utf-8"><title>Fixture server</title>
<style>body{font:16px system-ui;background:#121411;color:#e8e6dd;padding:32px}a{color:#c9a961}</style>
</head><body>
<h1 id="hello">epicurus fixture server</h1>
<p id="session">session: ${restored ? "restored" : "new"}</p>
<p><a id="new-window" href="https://example.invalid/docs" target="_blank" rel="noopener">a new-window link</a></p>
<p><a id="elsewhere" href="${elsewhere}/sign-in">leave for another host</a></p>
<p><a id="file-link" href="file:///etc/hosts">a file: link</a></p>
</body></html>`);
}

function serveWeb(req: IncomingMessage, res: ServerResponse, distDir: string, path: string): void {
  const dist = resolve(distDir);
  if (path.startsWith("/platform/")) {
    if (path === "/platform/v1/auth/session") {
      json(res, 200, {
        mode: "none",
        signed_in: false,
        provider_name: null,
        auto_redirect: false,
        user: null,
        expires_at: null,
      });
      return;
    }
    json(res, 404, { detail: "Not served by the desktop smoke fixture." });
    return;
  }
  const safe = normalize(path).replace(/^([/\\])+/, "");
  let file = join(dist, safe);
  try {
    if (!file.startsWith(dist + sep) || !statSync(file).isFile()) file = join(dist, "index.html");
  } catch {
    file = join(dist, "index.html");
  }
  res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
  res.end(req.method === "HEAD" ? undefined : readFileSync(file));
}

export async function startFixture(options: FixtureOptions = {}): Promise<Fixture> {
  const kind = options.kind ?? "epicurus";
  const requests: string[] = [];
  const server: Server = createServer((req, res) => {
    const path = new URL(req.url ?? "/", "http://fixture").pathname;
    requests.push(`${req.method ?? "GET"} ${path}`);
    if (kind === "other") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end("<!doctype html><title>Not epicurus</title><h1 id='other'>some other server</h1>");
      return;
    }
    if (path === "/platform/v1/info") {
      json(res, 200, INFO);
      return;
    }
    if (kind === "web" && options.webDist) {
      serveWeb(req, res, options.webDist, path);
      return;
    }
    if (path === "/" || path === "/index.html") {
      fixturePage(req, res, options.elsewhere ?? "http://127.0.0.1:9");
      return;
    }
    if (path === "/sw.js") {
      res.writeHead(200, { "content-type": "text/javascript" });
      res.end("self.addEventListener('push', () => {});");
      return;
    }
    if (path === "/sign-in") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end("<!doctype html><title>Identity provider</title><h1 id='idp'>sign in here</h1>");
      return;
    }
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found");
  });
  await new Promise<void>((resolve) => server.listen(options.port ?? 0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  return {
    origin: `http://127.0.0.1:${port}`,
    port,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}
