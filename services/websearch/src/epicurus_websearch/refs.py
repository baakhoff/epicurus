"""Stable references for web-search results and ingested links (#551, #739, ADR-0019).

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
# An ingested link (#739). A second kind rather than a reused ``result``: the two carry
# different facts (a result has an engine and a snippet; a source has a media kind and a
# site), so one shared payload would leave half the hover-card's fields empty either way.
SOURCE_KIND = "source"


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


def _encode(payload: dict[str, str]) -> str:
    """base64url of a compact JSON object, padding stripped."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(ref_id: str) -> dict[str, object]:
    """Decode a ``ref_id`` payload; 400 on anything malformed or unsafely-schemed.

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
    return payload


def encode_ref(*, url: str, title: str, snippet: str, engine: str) -> str:
    """Encode a search result into an opaque, URL-safe ``ref_id``."""
    return _encode(
        {"url": canonical_url(url), "title": title, "snippet": snippet, "engine": engine}
    )


def decode_ref(ref_id: str) -> dict[str, str]:
    """Decode a search-result ``ref_id`` back to its fields; 400 on anything malformed."""
    payload = _decode(ref_id)
    return {
        "url": str(payload["url"]),
        "title": str(payload.get("title") or ""),
        "snippet": str(payload.get("snippet") or ""),
        "engine": str(payload.get("engine") or ""),
    }


def encode_source_ref(*, url: str, title: str, summary: str, kind: str, site: str) -> str:
    """Encode an ingested link (#739) into an opaque, URL-safe ``ref_id``.

    Same stateless codec as a search result, different payload: ``kind`` is the ingest kind
    (``article`` / ``image`` / ``video`` / ``page`` / ``unreachable``) and ``site`` the
    publication, which is what the hover-card shows instead of an engine.
    """
    return _encode(
        {
            "url": canonical_url(url),
            "title": title,
            "summary": summary,
            "kind": kind,
            "site": site,
        }
    )


def decode_source_ref(ref_id: str) -> dict[str, str]:
    """Decode an ingested-link ``ref_id`` back to its fields; 400 on anything malformed."""
    payload = _decode(ref_id)
    return {
        "url": str(payload["url"]),
        "title": str(payload.get("title") or ""),
        "summary": str(payload.get("summary") or ""),
        "kind": str(payload.get("kind") or ""),
        "site": str(payload.get("site") or ""),
    }
