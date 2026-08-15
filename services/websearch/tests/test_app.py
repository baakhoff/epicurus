"""Smoke tests — the ASGI app builds and exposes the expected routes."""

from __future__ import annotations

import base64
import json
import os

from fastapi.testclient import TestClient

from epicurus_core import route_paths
from epicurus_websearch.refs import encode_ref, encode_source_ref

os.environ.setdefault("SEARXNG_URL", "http://localhost:8080")
os.environ.setdefault("PLATFORM_URL", "http://localhost:8080")


def test_app_exposes_ops_mcp_manifest_and_status_routes() -> None:
    from epicurus_websearch.app import create_app

    app = create_app()
    paths = route_paths(app)
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/manifest" in paths
    assert "/status" in paths
    assert "/resolve/result/{ref_id}" in paths
    assert "/resolve/source/{ref_id}" in paths
    assert any(p.startswith("/mcp") for p in paths)


class TestResolveResult:
    """HTTP tests for the stateless hover-card resolver (#551, ADR-0019)."""

    def test_returns_hovercard(self) -> None:
        from epicurus_websearch.app import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        ref = encode_ref(
            url="https://example.com/page", title="Title", snippet="Snip", engine="google"
        )
        resp = client.get(f"/resolve/result/{ref}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Title"
        assert body["description"] == "Snip"
        details = {d["label"]: d["value"] for d in body["details"]}
        assert details["Engine"] == "google"
        assert details["Domain"] == "example.com"
        assert body["href"] == {"label": "Open page", "url": "https://example.com/page"}

    def test_malformed_ref_is_400(self) -> None:
        from epicurus_websearch.app import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        resp = client.get("/resolve/result/not-valid-base64-!!!")
        assert resp.status_code == 400

    def test_non_http_scheme_ref_is_400_not_500(self) -> None:
        from epicurus_websearch.app import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        payload = json.dumps({"url": "javascript:alert(1)", "title": "x"})
        bad = base64.urlsafe_b64encode(payload.encode()).decode("ascii").rstrip("=")
        resp = client.get(f"/resolve/result/{bad}")
        assert resp.status_code == 400
        assert resp.json()["detail"] != "javascript:alert(1)"


class TestResolveSource:
    """HTTP tests for the ingested-link hover-card resolver (#739, ADR-0019)."""

    def test_returns_hovercard_with_kind_and_site(self) -> None:
        from epicurus_websearch.app import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        ref = encode_source_ref(
            url="https://coastalreview.example/a/tidal",
            title="Tidal turbines",
            summary="A five-turbine array…",
            kind="article",
            site="The Coastal Review",
        )
        resp = client.get(f"/resolve/source/{ref}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Tidal turbines"
        details = {d["label"]: d["value"] for d in body["details"]}
        assert details["Kind"] == "article"
        assert details["Site"] == "The Coastal Review"
        assert body["href"]["url"] == "https://coastalreview.example/a/tidal"

    def test_site_falls_back_to_the_domain(self) -> None:
        from epicurus_websearch.app import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        ref = encode_source_ref(
            url="https://example.com/x", title="T", summary="", kind="page", site=""
        )
        body = client.get(f"/resolve/source/{ref}").json()
        details = {d["label"]: d["value"] for d in body["details"]}
        assert details["Site"] == "example.com"

    def test_tampered_ref_is_400_not_500(self) -> None:
        from epicurus_websearch.app import create_app

        client = TestClient(create_app(), raise_server_exceptions=True)
        payload = json.dumps({"url": "javascript:alert(1)", "kind": "article"})
        bad = base64.urlsafe_b64encode(payload.encode()).decode("ascii").rstrip("=")
        resp = client.get(f"/resolve/source/{bad}")
        assert resp.status_code == 400


def test_settings_searxng_url_from_env() -> None:
    import importlib

    import epicurus_websearch.settings as mod

    importlib.reload(mod)
    from epicurus_websearch.settings import WebSearchSettings

    s = WebSearchSettings(service_name="websearch")
    assert "localhost" in s.searxng_url


def test_settings_max_results_default() -> None:
    from epicurus_websearch.settings import WebSearchSettings

    s = WebSearchSettings(service_name="websearch")
    assert s.websearch_max_results == 5


def test_link_ingest_caps_have_conservative_defaults() -> None:
    """#739: these bound a fetch of an operator-supplied URL made from inside the network."""
    from epicurus_websearch.settings import WebSearchSettings

    s = WebSearchSettings(service_name="websearch")
    assert s.link_ingest_max_bytes == 5_000_000
    assert s.link_ingest_timeout_s == 20.0
    assert s.link_ingest_max_redirects == 5
    assert s.link_ingest_max_text_chars == 20_000
    assert s.link_ingest_ytdlp is True
    assert s.link_ingest_vision_model == ""  # empty = let the core choose


def test_link_ingest_caps_are_operator_overridable(monkeypatch: object) -> None:
    from epicurus_websearch.settings import WebSearchSettings

    s = WebSearchSettings(
        service_name="websearch", link_ingest_max_bytes=1_000, link_ingest_ytdlp=False
    )
    assert s.link_ingest_max_bytes == 1_000
    assert s.link_ingest_ytdlp is False
