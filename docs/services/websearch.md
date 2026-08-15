# websearch

Self-hosted web search **and link reading** for the agent.  The websearch module runs a
[SearXNG](https://docs.searxng.org/) instance inside the stack and exposes two MCP tools —
`web_search` to *find* pages and `link_ingest` to *read* one.  No external API keys are
required.

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
| `web_search(query, num_results?)` | Search the web for `query`; returns up to `num_results` results (default: configured max, capped at 20) as a `ToolEnvelope`. The description steers the agent to search whenever a fact can't be grounded locally or may have changed since training (#703). |
| `link_ingest(url)` | Read one http(s) link and return what is behind it — kind, title, site, author, date, extracted text, image descriptions, and honest notes — as a `ToolEnvelope`. Guarded: private/loopback/internal addresses refused, every redirect hop re-validated, size/time/redirect/content-type capped (#739). |

#### `web_search` return shape

`web_search` returns a `ToolEnvelope` (ADR-0019, `epicurus_core.tool_envelope`):
`text` is a ranked listing — title, URL, engine, and snippet per result, capped
the same way as the entity-ref id block (`epicurus_core.capped_listing`) — fed
back to the model so it can still cite URLs directly; `entity_refs` carries one
`EntityRef` per (deduplicated) result (`module="websearch"`, `kind="result"`,
`summary` = snippet) so the UI renders chips. An empty/unreachable search
returns `tool_envelope("No web results found.", [])` rather than failing the turn.

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
| `GET` | `/status` | SearXNG reachability: `{"searxng_healthy": true, "searxng_url": "..."}`. |
| `GET` | `/resolve/result/{ref_id}` | Hover-card resolver for a search result (ADR-0019) — see below. |
| `GET` | `/resolve/source/{ref_id}` | Hover-card resolver for an ingested link (#739) — see below. |
| `*` | `/mcp/*` | Streamable-HTTP MCP transport (agent connects here). |

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
| `PLATFORM_URL` | `http://core-app:8080` | Core platform API. Used by `link_ingest` for image descriptions — the module holds no model keys (constraint #8). Wired but unused before v0.3.0. |
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

## Data model

The websearch module holds no persistent state.  It is a stateless proxy
between the agent and SearXNG.  SearXNG itself stores nothing — it fans out
queries to upstream engines on each request.  `link_ingest` adds no state either: nothing
it fetches is cached or written to disk (yt-dlp runs with `cachedir: False`), and both
hover-card kinds resolve from self-describing `ref_id`s rather than a store.

## Dependencies

| Service | Why |
| ------- | --- |
| SearXNG | The search backend; must be healthy before the module starts. |
| NATS | Event bus (connected at startup; no events are used in v0.1). |
| core-app | Platform API. `link_ingest` asks the core's LLM gateway to describe images (constraint #8) — the module holds no model keys. Optional in practice: with the core unreachable, or with no vision-capable model configured, ingestion still returns text and metadata plus a note explaining the missing description. |
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
