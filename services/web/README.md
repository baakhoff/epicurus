# Web shell (`epicurus-web`)

The epicurus **web UI shell** (ADR-0007) — chat with the agent, manage models and
providers, flip the power state, and configure modules. It is its own container
behind the edge gateway, **phone-first**, and installable as a **PWA**.

It is a *shell* on purpose: modules surface their UI **declaratively from their
manifest** (a config form as JSON Schema, status, and actions that invoke the
module's MCP tools through the core). Installing a module makes its panel appear —
**no UI rebuild, and no module JavaScript ever runs in the shell** (ADR-0007 Tier 1;
rich Tier-2 iframes are reserved in the manifest but not rendered yet).

## What's inside

| Screen | What it does |
| --- | --- |
| **Chat** | Streaming agent conversations (SSE deltas + live tool-call chips), markdown rendering, per-chat model picker, session sidebar backed by cross-chat memory (`session_id`), delete/forget. |
| **Models** | Local Ollama models: pull with live progress (SSE), delete, see size/family; hosted providers: status and **API-key entry** (stored core → OpenBao, never in the browser). |
| **Modules** | Every installed module: health, manifest summary, **auto-rendered config form** (JSON Schema), and manifest-declared **actions** (invoke the module's tools). |
| **Settings** | Theme (dark/light/system), default model, display preferences. |

The **power orb** lives in the shell header on every screen (ADR-0005): one tap
pauses (unloads models, suspends inference), one tap resumes. Its color reflects
`active` / `idle` / `paused` live.

## Architecture

- **Vite + React + TypeScript** (strict), Tailwind v4, vendored shadcn-style
  components in `src/components/ui.tsx`, lucide icons, Zustand stores,
  TanStack Query for server state, zod-validated API contracts
  (`src/lib/contracts.ts` mirrors the core's pydantic models).
- **Local-first, zero CDN:** fonts (Inter, JetBrains Mono, Literata) and every
  asset are vendored and built in. The nginx CSP (`connect-src 'self'`) enforces
  it — the app can only ever talk to its own origin.
- **Serving:** a multi-stage build → static files on an **unprivileged nginx**
  that also reverse-proxies `/platform/` to the core (`CORE_APP_URL`) — the API
  is **same-origin** (no CORS), SSE passes through unbuffered, and the proxy
  resolves the core at request time so the UI stays up while the core restarts.
- **PWA:** installable manifest + icons, offline-cached shell (service worker),
  `/platform` explicitly excluded from the service worker so streams always hit
  the network. Updates are prompt-based, never silent.

## Develop

Against a running stack (the dev server proxies `/platform` to the core on
`localhost:8082`):

```bash
cd services/web
npm ci
npm run dev        # http://localhost:5173
```

Gates (CI runs the same): `npm run lint`, `npm run build` (includes `tsc -b`),
`npm test` (vitest). Icons are generated from the logo SVG via `npm run icons`.

## Run in the stack

Wired into the top-level `compose.yaml`, so it comes up with the stack:

```bash
docker compose up -d web
```

Routed by the edge gateway at `web.localhost` **and** as the lowest-priority
catch-all — so from a phone on your LAN/VPN, `http://<host>:8088/` is the UI with
no host alias needed. Reachable directly (loopback) on `${WEB_PORT:-8084}`.

| Env | Default | Meaning |
| --- | --- | --- |
| `CORE_APP_URL` | `http://core-app:8080` | Where nginx proxies `/platform/`. |
| `WEB_PORT` | `8084` | Host port (loopback-bound by default). |
