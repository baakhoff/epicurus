"""Stable references for web-search results (#551, ADR-0019).

A search result is named to the rest of the platform by an opaque, URL-safe
``ref_id`` that self-describes the result — the module holds no store, so the
hover-card resolver must reconstruct a result's title, snippet, engine, and URL
from the ref_id alone, including for a session reopened days after the search
ran (``epicurus_knowledge.refs`` is the same stateless-entity pattern, for a
module with a two-field ``source:path`` identity; a search result needs four
fields, so this uses JSON rather than a delimiter join).

The encoding is base64url of a compact JSON object. base64url (not raw JSON,
not hex) because the core proxies resolves at ``GET /resolve/{kind}/{ref_id}``,
where ``ref_id`` must survive as a single path segment matching ``[^/]+``; the
result URL alone can contain ``/``, ``?``, and non-ASCII characters that
wouldn't.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

RESULT_KIND = "result"


def canonical_url(url: str) -> str:
    """Normalize trivial formatting differences so the same page always dedupes.

    Lowercases the scheme/host, drops a trailing ``/`` on the path, and strips
    any fragment (never sent to the server, never part of a page's identity).
    Two ``web_search`` calls that surface the same page — even phrased
    slightly differently by different engines — then encode to the same
    ``ref_id``, so the core's cross-call entity-ref dedup (``_RefCollector``)
    collapses them into one chip.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def encode_ref(*, url: str, title: str, snippet: str, engine: str) -> str:
    """Encode a search result into an opaque, URL-safe ``ref_id``."""
    canonical = canonical_url(url)
    payload = json.dumps(
        {"url": canonical, "title": title, "snippet": snippet, "engine": engine},
        separators=(",", ":"),
    )
    raw = payload.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_ref(ref_id: str) -> dict[str, str]:
    """Decode a ``ref_id`` back to its result fields; 400 on anything malformed.

    A bad id is a client error, not a server error — it reaches us only
    through a user- or agent-supplied reference, so it is never trusted.
    Rejects a decoded payload whose URL isn't ``http(s)``, so a malformed or
    tampered ref_id can never surface as a ``javascript:`` (or other
    unsafe-scheme) ``href`` in a hover-card.
    """
    padding = "=" * (-len(ref_id) % 4)
    try:
        decoded = base64.urlsafe_b64decode(ref_id + padding).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="unknown reference") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="unknown reference")
    url = payload.get("url")
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="invalid reference scheme")
    return {
        "url": url,
        "title": str(payload.get("title") or ""),
        "snippet": str(payload.get("snippet") or ""),
        "engine": str(payload.get("engine") or ""),
    }
