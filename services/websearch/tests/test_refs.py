"""Tests for the stateless search-result ref codec (refs.py, #551)."""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from epicurus_websearch.refs import canonical_url, decode_ref, encode_ref


@pytest.mark.parametrize(
    ("url", "title", "snippet", "engine"),
    [
        ("https://example.com/page", "Title", "A snippet.", "google"),
        # Unicode in every field, including the URL's path.
        ("https://example.com/wiki/Caf%C3%A9", "Café — a résumé", "naïve café ☕", "duckduckgo"),
        # A very long URL with a query string.
        (
            "https://example.com/search?" + "&".join(f"q{i}=value{i}" for i in range(200)),
            "Long query",
            "snippet",
            "bing",
        ),
        ("https://example.com/", "Root", "", "google"),
    ],
)
def test_encode_decode_round_trip(url: str, title: str, snippet: str, engine: str) -> None:
    ref = encode_ref(url=url, title=title, snippet=snippet, engine=engine)
    assert "/" not in ref  # slash-free → survives a single URL path segment
    assert "=" not in ref  # padding stripped
    decoded = decode_ref(ref)
    assert decoded["url"] == canonical_url(url)
    assert decoded["title"] == title
    assert decoded["snippet"] == snippet
    assert decoded["engine"] == engine


def test_canonical_url_normalizes_case_and_trailing_slash() -> None:
    assert canonical_url("HTTPS://Example.COM/Path/") == "https://example.com/Path"
    assert canonical_url("https://example.com") == "https://example.com/"


def test_canonical_url_drops_fragment_keeps_query() -> None:
    assert canonical_url("https://example.com/p?x=1#section") == "https://example.com/p?x=1"


def test_encode_same_result_twice_is_deterministic() -> None:
    ref_a = encode_ref(url="https://a.com/x", title="T", snippet="S", engine="google")
    ref_b = encode_ref(url="https://a.com/x", title="T", snippet="S", engine="google")
    assert ref_a == ref_b


def test_encode_same_url_trailing_slash_variant_dedupes() -> None:
    ref_a = encode_ref(url="https://a.com/x", title="T", snippet="S", engine="google")
    ref_b = encode_ref(url="https://a.com/x/", title="T", snippet="S", engine="google")
    assert ref_a == ref_b


def test_decode_rejects_non_base64() -> None:
    with pytest.raises(HTTPException) as err:
        decode_ref("%%% not base64 %%%")
    assert err.value.status_code == 400


def test_decode_rejects_valid_base64_non_json() -> None:
    bad = base64.urlsafe_b64encode(b"not json at all").decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as err:
        decode_ref(bad)
    assert err.value.status_code == 400


def test_decode_rejects_json_that_is_not_an_object() -> None:
    bad = base64.urlsafe_b64encode(b'["just", "a", "list"]').decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as err:
        decode_ref(bad)
    assert err.value.status_code == 400


@pytest.mark.parametrize(
    "scheme_payload",
    [
        b'{"url": "javascript:alert(1)", "title": "x", "snippet": "", "engine": ""}',
        b'{"url": "ftp://example.com/f", "title": "x", "snippet": "", "engine": ""}',
        b'{"url": "data:text/html,evil", "title": "x", "snippet": "", "engine": ""}',
        b'{"title": "x", "snippet": "", "engine": ""}',  # no url key at all
    ],
)
def test_decode_rejects_non_http_scheme(scheme_payload: bytes) -> None:
    bad = base64.urlsafe_b64encode(scheme_payload).decode("ascii").rstrip("=")
    with pytest.raises(HTTPException) as err:
        decode_ref(bad)
    assert err.value.status_code == 400
