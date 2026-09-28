# websearch

Web search **and link reading** for the agent.  The websearch module runs a
[SearXNG](https://docs.searxng.org/) instance inside the stack and exposes two MCP tools —
`web_search` to *find* pages and `link_ingest` to *read* one.  SearXNG needs no external API
key and is the default. Once an OpenRouter key is stored on the Models page, the operator can
switch `web_search` to **OpenRouter's hosted web search** instead (v0.5.0, #984); the core runs
those searches with the tenant's key, which never reaches this module.

**v0.5.0** (#984, ADR-XXXX): a **search provider** choice on the Modules page — `searxng`
(default, unchanged behaviour) or `openrouter`. With `openrouter`, `web_search` asks the core
(`PlatformClient.web_search` → `POST /platform/v1/web-search`), which runs one search through
OpenRouter's `openrouter:web_search` server tool with the tenant's stored OpenRouter key and
returns normalised `{title, url, snippet, engine}` results; they get the same chips,
hover-cards (engine **OpenRouter**), dedupe and outcomes as SearXNG's. The option can only be
chosen once an OpenRouter key is stored: the shell greys it out and the core refuses to save
it (409 `provider_key_required`). If the key is removed afterwards, the tool says plainly that
no search ran and how to fix it; it does **not** fall back to SearXNG. The same change fixes a
long-standing gap: the Modules page's settings for this module (`websearch_max_results`,
`websearch_engines`) were stored by the core and **never read by the module**. It now reads
them through the core on each call (15-second cache), so a saved setting applies without a
restart. See [Stored settings](#stored-settings-984).

**v0.2.0** (#551, ADR-0019): `web_search` results now surface as entity-reference
chips in the chat UI, at parity with local sources (#333). Hover shows the title,
snippet, engine, and domain; clicking a chip opens the source page in a new tab —
websearch has no right-panel view of its own, so unlike every other resolver-backed
module it always carries an `href`. The module is (and stays) **stateless**: the
resolver reconstructs a result's hover-card entirely from a self-describing
`ref_id` (`epicurus_websearch.refs`, mirroring `epicurus_knowledge.refs`'s pattern),
not a lookup, so hover-cards keep resolving in sessions reopened long after the
search ran. Same-page duplicates within one `web_search` call (SearXNG returning
one URL from multiple engines) are collapsed before refs are built; two separate
calls surfacing the same result encode to the same `ref_id`, so the core's
cross-call entity-ref dedup (`_RefCollector`) merges them into one chip.

**v0.2.1** (#703): the tool description now tells the agent *when* to reach for
the web — whenever a fact cannot be grounded in the operator's own data or may
have changed since training — matching the source-grounding ladder the core's
default agent instructions gained in the same change. The tool's behavior is
unchanged.

**v0.4.0** (#936, #920, ADR-0139): `web_search` no longer folds every failure mode into a
plain "no results." SearXNG's `/search?format=json` response carries an `unresponsive_engines`
field — `[engine, error_type]` pairs for engines that timed out, were blocked, or got
rate-limited — that the client previously discarded, making a genuine zero-hit query
indistinguishable from every engine failing at once. `SearXNGClient.search()` now returns a
`SearchOutcome` (results **plus** `unresponsive_engines`/`number_of_results`), the tool
distinguishes three outcomes (results, genuinely empty, degraded), a WARNING names the
unresponsive engines, and `GET /status` gains a `degraded` flag. The tool also stopped
catching every exception `SearXNGClient.search()` can raise: a genuine SearXNG failure (down,
erroring, unreachable) now reaches the model as an actionable `ToolError` through the
ADR-0136 tool-error seam instead of a silent empty envelope — which is also why `httpx`'s
`HTTPStatusError`/`TransportError` joined `epicurus-core`'s anticipated-exception set in the
same change (#920): a provider HTTP error is expected traffic, not a crash, so it is logged
at WARNING rather than ERROR-with-traceback.

**v0.3.0** (#739, ADR-0120): a second tool, **`link_ingest`**. Search could find a page;
nothing in the platform could *read* one. `link_ingest(url)` fetches an operator-supplied
link under a purpose-built SSRF guard and returns its substance — an article's byline and
body text, a direct image's description, a public video's metadata and the uploader's own
subtitles. All of it stays inside websearch (the roadmap forbids new modules before 1.0),
the module calls no other module (ADR-0004 — filing the result into the knowledge base is
the *agent's* next step, via `knowledge_propose_edit`), and the one model call it can make
goes through the core's LLM gateway (constraint #8). The same change added a **vision gate
to `POST /platform/v1/chat`** in the core, so a module sending image parts to a model
without vision gets a clean structured 400 instead of a silent ignore — see
[platform API](../reference/platform-api.md#post-platformv1chat).

## What it is

The module adds two containers to the stack:

- **SearXNG** (`infra/searxng/`) — a privacy-preserving metasearch engine.
  Internal-only: reachable at `http://searxng:8080` on the Docker network; no
  host port is published by default.
- **websearch** (`services/websearch/`) — a FastAPI service that wraps SearXNG
  with the standard epicurus module contract (MCP, manifest, health, metrics).

## Contract

### MCP tools

| Tool | Description |
| ---- | ----------- |
| `web_search(query, num_results?)` | Search the web for `query` with the operator's chosen provider (SearXNG, or OpenRouter through the core); returns up to `num_results` results (omitted = the configured max, capped at 20) as a `ToolEnvelope`. `num_results` is optional (`null` default) since v0.5.0 so the configured max is read at call time. The description steers the agent to search whenever a fact can't be grounded locally or may have changed since training (#703). |
| `link_ingest(url)` | Read one http(s) link and return what is behind it — kind, title, site, author, date, extracted text, image descriptions, and honest notes — as a `ToolEnvelope`. Guarded: private/loopback/internal addresses refused, every redirect hop re-validated, size/time/redirect/content-type capped (#739). |

#### `web_search` return shape

`web_search` returns a `ToolEnvelope` (ADR-0019, `epicurus_core.tool_envelope`):
`text` is a ranked listing — title, URL, engine, and snippet per result, capped
the same way as the entity-ref id block (`epicurus_core.capped_listing`) — fed
back to the model so it can still cite URLs directly; `entity_refs` carries one
`EntityRef` per (deduplicated) result (`module="websearch"`, `kind="result"`,
`summary` = snippet) so the UI renders chips.

**Three distinguishable outcomes (#936, ADR-0139)**, since folding them into one shape hid a
degraded search behind a clean "nothing found":

| Outcome | Shape |
| ------- | ----- |
| Results found | The listing above; if SearXNG's response also carried `unresponsive_engines`, a trailing note names them and says the results may be incomplete. |
| Genuinely empty (`results: []`, `unresponsive_engines: []`) | `tool_envelope("No web results found.", [])` — unchanged from before v0.4.0. |
| Degraded (`results: []`, `unresponsive_engines` non-empty) | A distinct envelope stating plainly that this is **not a confirmed empty result** — SearXNG answered but one or more engines did not — naming which engines and why, so the model reports "search is degraded/unreliable" rather than narrating a clean empty search. |
| SearXNG unreachable or erroring | `SearXNGClient.search()`'s exception is no longer caught here — it reaches the ADR-0136 tool-error seam and returns to the model as a `ToolError` carrying the real reason, logged at WARNING (see `httpx.HTTPStatusError`/`TransportError` in `epicurus-core`'s anticipated-exception set, #920) rather than a silent `[]`. |

A WARNING is logged (naming the unresponsive engines and their error types) whenever
`unresponsive_engines` is non-empty, independent of whether the operator happens to ask about
it — so the condition is visible in the container log on its own.

**With the OpenRouter provider (#984)** the same contract holds, read from the core's
`WebSearchResult`:

| Outcome | Shape |
| ------- | ----- |
| Results found | The same listing and chips; each result's engine is `OpenRouter`, so the hover-card's **Engine** row reads "OpenRouter". Duplicate URLs collapse exactly as SearXNG's do. |
| Genuinely empty (`results: []`, `searched: true` or unknown) | `tool_envelope("No web results found.", [])`. |
| Degraded (`results: []`, `searched: false`) | A distinct envelope: OpenRouter answered but its search model did not run a search, so this is **not a confirmed empty result**. |
| No OpenRouter key stored (core answers 409 `openrouter_key_missing`) | A plain envelope saying web search is set to OpenRouter, no key is stored, **no search ran**, and the operator should add a key on the Models page or switch back to SearXNG. Not an error and not an empty result; there is no fallback to SearXNG. |
| OpenRouter or the core failing (any other `PlatformError`: `openrouter_key_rejected`, `provider_error`, `provider_unreachable`, `key_store_unavailable`) | Raised through the ADR-0136 tool-error seam; the model reads the core's sentence (e.g. "OpenRouter web search failed (402): Insufficient credits"), logged at WARNING (`PlatformError` is in `epicurus-core`'s anticipated set). |

#### `link_ingest` return shape

`link_ingest` returns a `ToolEnvelope` whose `text` is a rendered document — a labelled
block rather than raw JSON, because the agent's next move is to write prose about it and
often to file it, and headings survive being quoted into a knowledge document. `entity_refs`
carries exactly one `EntityRef` for the source (`module="websearch"`, `kind="source"`).

The fields behind the rendering:

| Field | Meaning |
| ----- | ------- |
| `kind` | `article` · `image` · `video` · `page` · `unreachable`. |
| `url` | The **final** URL after redirects — what was actually read, and what to cite. |
| `title` / `site` | From OpenGraph / oEmbed / yt-dlp, in that order of trust per tier. |
| `author` / `published` | Byline and publication date when the source exposes them; `null` otherwise. |
| `text` | The extracted body. For a video this is the description, plus the uploader's subtitles under a `Captions (<lang>, published by the uploader):` heading. |
| `image_descriptions` | Descriptions produced by the **core's** vision model — the image itself for a direct image link, the poster frame (prefixed `Thumbnail: `) for a video. Empty when no vision model is configured. |
| `transcript` | **Reserved and always empty.** ASR is a new gateway modality, deferred to milestone 5.0.0 (#739); the field exists now so it can land without a contract change. Uploader subtitles are *not* a transcript and never appear here. |
| `notes` | What was and was not retrieved, in operator-readable prose. The agent is told to relay these rather than fill the gaps. |
| `retrieved_at` | UTC date of the fetch — the agent embeds it in anything it files. |
| `ok` | `false` for `unreachable`. |

**A failure is a result, not an exception.** A login wall, a dead host, a refused private
address, a PDF, a captionless video, an image no model could see — each comes back as a
well-formed result carrying a note. Nothing here ends the turn.

#### `link_ingest` tiers

1. **HTML / articles.** Guarded fetch, then [trafilatura](https://trafilatura.readthedocs.io/)
   for readability-grade main-text extraction *and* metadata (OpenGraph / JSON-LD / `<meta>`).
   One dependency covers both halves; `readability-lxml` would have needed a soup parser
   alongside it for the metadata. Apache-2.0, pure Python over `lxml`, no OS packages — the
   Dockerfile is unchanged. A body under 400 characters is reported as a `page`, not an
   `article`, rather than presenting a paywall teaser as the piece.
2. **Images.** The bytes are fetched (SSRF-guarded, size-capped) and described by the core's
   vision model, reached as OpenAI-style content parts over `PlatformClient.chat` — the
   module holds no model access or keys (constraint #8). When the core refuses because the
   resolved model has no vision (the new structured 400), the result degrades to metadata
   plus a note naming the model and the fix. A truncated image is never sent: half a JPEG
   describes nothing.
3. **Video / reels / audio.** Metadata and *uploader-published* subtitles only — nothing is
   downloaded, nothing is transcribed, and no ffmpeg or OS package is involved. Sources, in
   order of trust: yt-dlp (`extract_info(download=False)`), the platform's token-free oEmbed
   endpoint, then OpenGraph off the public page. The platform's own machine captions
   (`automatic_captions`) are deliberately **not** used — that is ASR by another name, and
   the result says when they were the only thing on offer. Instagram and Facebook have no
   token-free oEmbed since Meta retired theirs in 2020, so those links rely on OpenGraph,
   exactly as a signed-out browser would.

#### `link_ingest` safety policy (SSRF)

This is the first tool in the platform that fetches an arbitrary URL *from inside the Docker
network*, where the core, Postgres, Valkey, Qdrant, OpenBao, and the docker proxy all answer
without authentication. Nothing server-side existed to reuse, so the guard
(`epicurus_websearch.safety`) is built here. In order:

| Check | Rule |
| ----- | ---- |
| Scheme | `http` / `https` only — no `file:`, `ftp:`, `gopher:`, `data:`. |
| Credentials | A `user:pass@host` URL is refused outright. Nothing here ever authenticates. |
| Hostname shape | A single-label host (`core-app`, `nats`, `qdrant`) is refused — on this network that *is* a service name, and a public URL always has a dot. So are `.localhost`, `.local`, `.internal`, `.localdomain`, `.home.arpa`, and `.onion`. |
| Address | The host is resolved and **every** returned address is checked against private / loopback / link-local / reserved / multicast / CGNAT ranges in both families, with IPv4-mapped (`::ffff:127.0.0.1`) and 6to4-wrapped IPv6 unwrapped first. A host answering with a mix of public and private addresses is refused, not partially allowed. |
| Redirects | Followed **manually** (`follow_redirects=False`) so every hop goes through all of the above before it is requested. A public URL that 302s to `169.254.169.254` is the classic SSRF, and the hop is what matters. |
| Caps | Bytes, wall-clock across all hops, redirect count, and an allow-list of content types. Over-long bodies are truncated and flagged rather than failing; a truncated image is refused by the caller. |
| yt-dlp | Runs its own HTTP outside the guarded client, so it is restricted to an **allow-list** of known public media platforms and only ever sees a URL that already passed the guard. No cookies, no cookie jar, no netrc, no credentials. Subtitle URLs it returns are fetched back through the guarded fetcher. |

**Residual limitation, stated plainly.** The guard resolves the hostname and httpx then
resolves it again to connect, so a DNS record that changes between the two (rebinding) is
not caught. Closing that needs connect-time pinning of the validated address, which httpx
does not expose without a custom transport; it is a deliberate v1 gap. Every non-DNS vector
above *is* closed, and each is covered by a test.

**Honesty rules (#739), enforced structurally rather than promised.** No login walls, no
credentials, no CAPTCHA circumvention. A private or login-only link returns
`kind: "unreachable"` with a note saying the assistant never signs in — never a guess at
what the page might have said.

### HTTP endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/health` | Liveness probe (standard epicurus health response). |
| `GET` | `/metrics` | Prometheus metrics. |
| `GET` | `/manifest` | Module manifest (tools, UI, config schema). |
| `GET` | `/status` | SearXNG reachability **and** last-search health (#936), plus the active provider (#984): `{"backend": "searxng", "openrouter_last_result": null, "searxng_healthy": true, "searxng_url": "...", "search_evidence": "no search has run since this instance started", "degraded": false, "unresponsive_engines": null}`. `backend` is the provider the next search will use (`searxng` / `openrouter`, from the stored setting); `openrouter_last_result` is `null` until an OpenRouter-backed search has run in this process, then `results` / `no results` / `no search ran` / `no OpenRouter key stored` / `failed: <reason>`. The `searxng_*`, `degraded` and `unresponsive_engines` fields always describe SearXNG, whichever provider is active. Every value is a flat scalar — the core proxies the object verbatim and the Modules panel stringifies each field, so `unresponsive_engines` is the rendered `"google (timeout), bing (blocked)"` (or `null`), never a nested list. `searxng_healthy` is `/healthz` liveness — true as long as the SearXNG process is up, even if every engine it asks is blocked. `degraded`/`unresponsive_engines` report the **most recent `web_search` call's** engine health (`SearXNGClient.last_unresponsive_engines`), not a separate probe, and `search_evidence` says whether there has been a search to report on at all — see the note below. |
| `GET` | `/resolve/result/{ref_id}` | Hover-card resolver for a search result (ADR-0019) — see below. |
| `GET` | `/resolve/source/{ref_id}` | Hover-card resolver for an ingested link (#739) — see below. |
| `*` | `/mcp/*` | Streamable-HTTP MCP transport (agent connects here). |

**Why `/status` piggybacks on real traffic instead of a canary query (#920).** A cached
canary — a fixed low-cost query on an interval, distinguishing "process up" from "process
up, zero engines answering" — was the other option. Rejected: the exact failure mode this
exists to catch is engines being rate-limited or blocked, and a canary would mean the module
polling those same engines on a schedule purely to test them, competing with the operator's
own searches for the same limited budget on the engines already flagged as the problem. The
operator's actual use of `web_search` is a free, always-fresh signal, and the module makes no
other request to SearXNG regardless — so `/status` reads `SearXNGClient.last_unresponsive_engines`,
set by the most recent `search()` call, at zero extra cost. The tradeoff: an instance nobody
has searched with since it broke still reads `degraded: false` until the next search. Accepted
— `/status` exists to explain the *next* result, not to poll for outages independent of use —
but stated on the panel rather than left for the reader to infer: `search_evidence` says
whether any search has run in this process, so `degraded: false` with nothing behind it reads
as the absence of evidence it is, and not as a clean bill of health.

#### `HoverCard` shape (from resolver)

Stateless: every field is decoded straight out of `ref_id`, never looked up.
Always carries an `href` — the chip's only destination is the source page itself,
opened in a new tab (`rel="noopener noreferrer"`, core-rendered via `CardLink`).
A malformed or tampered `ref_id` (bad base64, non-JSON, or a non-`http(s)` URL —
e.g. a `javascript:` scheme) is rejected with **400**, never a 500 and never
echoed back as an `href`.

```json
{
  "title": "Page title",
  "description": "Brief description from the search result",
  "details": [
    { "label": "Engine", "value": "google" },
    { "label": "Domain", "value": "example.com" }
  ],
  "href": { "label": "Open page", "url": "https://example.com/page" }
}
```

The core exposes this via `GET /platform/v1/modules/websearch/resolve/result/{ref_id}`.

#### Ingested-link `HoverCard` (`kind = "source"`, #739)

Same stateless codec, a different payload: a search result has an engine and a snippet, an
ingested link has a media *kind* and a site, so one shared payload would leave half of
either card empty. A tampered or non-`http(s)` `ref_id` is rejected with **400**, exactly as
for a result.

```json
{
  "title": "Tidal turbines feed a village for a winter",
  "description": "A five-turbine array ran a village of 340 through the winter.",
  "details": [
    { "label": "Kind", "value": "article" },
    { "label": "Site", "value": "The Coastal Review" }
  ],
  "href": { "label": "Open page", "url": "https://coastalreview.example/a/tidal" }
}
```

### Events

The websearch module emits and consumes no NATS events.

## Configuration

### websearch service

| Environment variable | Default | Description |
| -------------------- | ------- | ----------- |
| `SEARXNG_URL` | `http://searxng:8080` | Base URL of SearXNG on the internal network. |
| `PLATFORM_URL` | `http://core-app:8080` | Core platform API. Used by `link_ingest` for image descriptions, to read the stored settings, and for OpenRouter-backed searches (#984) — the module holds no keys (constraints #4, #8). Wired but unused before v0.3.0. |
| `WEBSEARCH_MAX_RESULTS` | `5` | Default maximum results per search (operator override). |
| `WEBSEARCH_ENGINES` | _(empty)_ | Comma-separated SearXNG engine names. Empty = SearXNG defaults. |
| `LINK_INGEST_MAX_BYTES` | `5000000` | Hard ceiling on bytes read per fetch. A longer body is truncated and flagged, not failed; a truncated *image* is refused rather than described. |
| `LINK_INGEST_TIMEOUT_S` | `20.0` | Wall-clock budget for one fetch, **including every redirect hop**. Also bounds the yt-dlp probe. |
| `LINK_INGEST_MAX_REDIRECTS` | `5` | Redirect hops followed. Each is re-validated by the SSRF guard before it is taken. |
| `LINK_INGEST_MAX_TEXT_CHARS` | `20000` | Characters of extracted text (and of captions) kept per ingest. |
| `LINK_INGEST_YTDLP` | `true` | Run yt-dlp for richer metadata/subtitles on allow-listed public media platforms. `false` keeps tier 3 on oEmbed + OpenGraph only. |
| `LINK_INGEST_VISION_MODEL` | _(empty)_ | Model used for image descriptions. Empty means the core's configured default; the core resolves, gates, and meters the call either way. |
| `LINK_INGEST_USER_AGENT` | `epicurus-websearch (+https://github.com/baakhoff/epicurus)` | Sent on every ingest fetch. Identifies the requests rather than impersonating a browser — name plus a contact URL is what site bot policies ask for, and some hosts (Wikimedia) reject a contactless agent outright. Point it at yourself for a public deployment. |
| `NATS_URL` | `nats://nats:4222` | NATS connection string. |
| `DEFAULT_TENANT_ID` | `local` | Tenant context. |
| `WEBSEARCH_PORT` | `8086` | Host port the module is published on (dev only). |

The `LINK_INGEST_*` caps all bound a fetch of an **operator-supplied URL made from inside
the stack network**, so the defaults are deliberately conservative. Raise them knowingly.

### Stored settings (#984)

The module's settings form on the Modules page (`config_schema`) has three fields. They are
stored by the core per tenant (`PUT /platform/v1/modules/websearch/config`, OpenBao
`modules/websearch/config`) and, since v0.5.0, **read back by the module** on each
`web_search` call through `epicurus_core.ModuleConfigCache` (a 15-second cache over
`PlatformClient.get_module_config`; the last good answer is kept while the core is
unreachable, and an empty answer means "env defaults"). A saved change applies within 15
seconds, no restart.

| Field | Values (default) | Effect |
| ----- | ---------------- | ------ |
| `websearch_backend` | `searxng` (default) · `openrouter` | Which provider `web_search` uses. `openrouter` carries `enumRequiresProviderKey: "openrouter"`: the shell disables it until the tenant's OpenRouter key is stored, and the core refuses to save it without one (409 `provider_key_required`; 503 `key_store_unavailable` when OpenBao cannot be asked). Unknown values read as `searxng`. |
| `websearch_max_results` | 1–20 (5) | Results per search when the agent does not pass `num_results`. Overrides `WEBSEARCH_MAX_RESULTS` when set to anything other than 5. |
| `websearch_engines` | text (empty) | SearXNG engines. Overrides `WEBSEARCH_ENGINES` when non-empty. Ignored by OpenRouter. |

**Precedence rule.** A stored value wins when it differs from the form's default; a stored
value equal to the default, or none, falls back to the env value. The form submits every
field, defaults included, so without this rule saving the form only to change the provider
would wipe an env-configured engine list. The cost: the form cannot set a field *back* to its
default over a non-default env value — change the env for that.

The OpenRouter search itself is configured on the **core** (`OPENROUTER_WEB_SEARCH_MODEL`,
`OPENROUTER_WEB_SEARCH_ENGINE`; see [config reference](../reference/config.md)), because the
core makes the call.

### SearXNG

SearXNG is configured via `infra/searxng/settings.yml`.  The defaults ship
with HTML and JSON output formats enabled and rate-limiting disabled (safe for
internal use).  Key settings to review before production:

| Setting | Location | Description |
| ------- | -------- | ----------- |
| `server.secret_key` | `infra/searxng/settings.yml` | Rotate this from the placeholder value. |
| Engines | `infra/searxng/settings.yml` | Uncomment or customise the engine list. |

Override the settings file by setting `SEARXNG_SETTINGS_FILE` in `.env` to
an absolute path on the host.

**Tuning note (#920): expect Google/Bing/DuckDuckGo-style engines to be rate-limited or
blocked.** A self-hosted instance sits behind a single egress IP with no browser
fingerprint — any VPS or Kubernetes deployment included — which is exactly what those
engines' anti-bot posture is built to catch. The stock default (SearXNG's own default
engine set, `WEBSEARCH_ENGINES` empty) is left as-is here; this is an operator tuning note,
not a behavior change. If `web_search` starts reporting degraded results (`GET /status`'s
`degraded` flag, or the WARNING logged whenever `unresponsive_engines` is non-empty), narrow
`WEBSEARCH_ENGINES` to engines with a looser anti-bot posture (e.g. `duckduckgo,brave,mojeek`)
rather than the full default set.

## Data model

The websearch module holds no persistent state.  It is a stateless proxy (its settings live in the core)
between the agent and SearXNG.  SearXNG itself stores nothing — it fans out
queries to upstream engines on each request.  `link_ingest` adds no state either: nothing
it fetches is cached or written to disk (yt-dlp runs with `cachedir: False`), and both
hover-card kinds resolve from self-describing `ref_id`s rather than a store.

## Dependencies

| Service | Why |
| ------- | --- |
| SearXNG | The search backend; must be healthy before the module starts. |
| NATS | Event bus (connected at startup; no events are used in v0.1). |
| core-app | Platform API. `link_ingest` asks the core's LLM gateway to describe images (constraint #8) — the module holds no model keys. Since v0.5.0 the module also reads its stored settings from the core and, with the OpenRouter provider chosen, sends every search to `POST /platform/v1/web-search`, where the core uses the tenant's OpenRouter key. With the core unreachable the module falls back to its env settings (SearXNG). |
| trafilatura | Article extraction + page metadata (Apache-2.0, pure Python over `lxml`). |
| yt-dlp | Public-platform video metadata and uploader subtitles. **Lazily imported** and failure-tolerant: strip it and tier 3 degrades to oEmbed + OpenGraph. Metadata only — nothing is ever downloaded, so no ffmpeg and no OS packages. |

## Run & extend

### Run locally (development)

Enable the module by ensuring both the searxng infra fragment and the websearch
module fragment are in the root `compose.yaml` include list (they are by
default).  Then:

```sh
task up
# or
docker compose up -d websearch searxng
```

The module is available at `http://localhost:8086` and SearXNG's UI at
`http://searxng.localhost` (via Traefik).

### Extend

- **Add engines**: edit `infra/searxng/settings.yml` and specify engines in the
  `engines:` section, or set `WEBSEARCH_ENGINES` to a comma-separated list.
- **Post-process results**: add a tool in `service.py` that calls `PlatformClient` to
  re-rank or summarise results — `link_ingest`'s captioner is the worked example.
- **Emit events**: add `module.emits(...)` declarations and publish via
  `EventBus` when a search completes.

### Extending `link_ingest` safely

The module splits into four files so each piece can be changed on its own:
`safety.py` (the guard and the bounded fetcher), `extract.py` (pure functions over bytes —
no network, no model, no state), `media.py` (the yt-dlp probe), `ingest.py` (which tier runs,
and how failures become notes).

- **A new platform**: add its host to `MEDIA_HOSTS` in `extract.py`, and its oEmbed endpoint
  to `OEMBED_ENDPOINTS` *only if that endpoint answers without a token* — #739 forbids
  authenticating, so a token-gated endpoint is not an option, it is a `None`.
- **A new content type**: widen `DEFAULT_ALLOWED_TYPES` in `safety.py` and add a branch in
  `LinkIngestor._ingest_web`. Anything not in the allow-list is refused with its type named.
- **Never loosen the guard to make a link work.** If a URL is refused, that is the answer.
  Every rule in the table above has a test in `tests/test_safety.py`; deleting one should
  fail the suite, which is the point.
- **Do not call another module from here** (ADR-0004). `link_ingest` returns the extract;
  saving it is the agent's job through the knowledge tools.
- **ASR stays out** until the gateway grows a transcription modality (milestone 5.0.0). When
  it does, it fills `IngestResult.transcript` — the field is already in the contract, and
  uploader subtitles must keep going to `text`, not there.
