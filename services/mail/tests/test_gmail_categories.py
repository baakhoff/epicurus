"""Gmail inbox category tabs — assembly, hiding rules, and the query mapping (#765)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from epicurus_core import PlatformClient
from epicurus_mail.gmail import GmailProvider


def _make_platform(access_token: str = "tok") -> PlatformClient:
    platform = MagicMock(spec=PlatformClient)
    platform.get_oauth_token = AsyncMock(return_value=access_token)
    return platform


def _resp(json_value: Any) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_value)
    return resp


def _mock_client() -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _provider() -> GmailProvider:
    return GmailProvider(platform=_make_platform(), tenant_id="local")


def _thread(subject: str, sender: str) -> dict[str, Any]:
    """A metadata ``threads.get`` body — one message, which is what a preview reads."""
    return {
        "id": "t",
        "messages": [
            {
                "labelIds": ["INBOX"],
                "snippet": "…",
                "internalDate": "1700000000000",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": subject},
                        {"name": "From", "value": sender},
                        {"name": "Date", "value": "Mon, 1 Jan 2024 10:00:00 +0000"},
                    ]
                },
            }
        ],
    }


def _wire(
    client: AsyncMock,
    *,
    newest: dict[str, str | None],
    threads: dict[str, dict[str, Any]] | None = None,
    unread: dict[str, int] | None = None,
    calls: list[tuple[str, Any]] | None = None,
) -> None:
    """Route Gmail GETs for a category probe/enrich run.

    *newest* maps a category id to the thread id its 1-result ``threads.list`` returns
    (``None`` = that category is empty); *threads* supplies each thread's metadata body and
    *unread* each ``CATEGORY_*`` label's count.
    """
    threads = threads or {}
    unread = unread or {}

    async def fake_get(url: str, **kwargs: Any) -> MagicMock:
        params = kwargs.get("params") or {}
        if calls is not None:
            calls.append((url, params))
        if url == "/users/me/threads":
            category = str(params["q"]).removeprefix("category:")
            thread_id = newest.get(category)
            return _resp({"threads": [{"id": thread_id}] if thread_id else []})
        if url.startswith("/users/me/threads/"):
            return _resp(threads[url.rsplit("/", 1)[1]])
        if url.startswith("/users/me/labels/"):
            label_id = url.rsplit("/", 1)[1]
            if label_id not in unread:
                raise httpx.HTTPError("no such label")
            return _resp({"messagesUnread": unread[label_id]})
        raise AssertionError(f"unexpected GET {url}")

    client.get = AsyncMock(side_effect=fake_get)


# ── the id -> query mapping (the single translation point) ────────────────────


@pytest.mark.parametrize(
    "category_id,expected",
    [
        ("primary", "category:primary"),
        ("promotions", "category:promotions"),
        ("social", "category:social"),
        ("updates", "category:updates"),
        ("forums", "category:forums"),
    ],
)
def test_category_query_maps_every_tab_id(category_id: str, expected: str) -> None:
    assert _provider().category_query(category_id) == expected


@pytest.mark.parametrize("bogus", ["", "PROMOTIONS", "category:promotions", "spam", "../x"])
def test_category_query_rejects_anything_that_isnt_a_tab_id(bogus: str) -> None:
    """An unknown id yields None so it can never be pasted into a Gmail query."""
    assert _provider().category_query(bogus) is None


# ── assembly ─────────────────────────────────────────────────────────────────


async def test_list_categories_builds_tabs_with_counts_and_previews() -> None:
    provider = _provider()
    client = _mock_client()
    _wire(
        client,
        newest={"primary": "tp", "promotions": "tm", "social": "ts", "updates": "tu"},
        threads={
            "tp": _thread("Lunch?", "alice@example.com"),
            "tm": _thread("50% off everything", "deals@shop.example"),
            "ts": _thread("You have 3 notifications", "noreply@social.example"),
            "tu": _thread("Your receipt", "billing@saas.example"),
        },
        unread={
            "CATEGORY_PERSONAL": 2,
            "CATEGORY_PROMOTIONS": 41,
            "CATEGORY_SOCIAL": 7,
            "CATEGORY_UPDATES": 3,
        },
    )
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    tabs = await provider.list_categories(label="INBOX")

    # Gmail's own tab order, Primary first.
    assert [t.id for t in tabs] == ["primary", "promotions", "social", "updates"]
    assert [t.title for t in tabs] == ["Primary", "Promotions", "Social", "Updates"]
    assert [t.unread for t in tabs] == [2, 41, 7, 3]
    promotions = tabs[1]
    assert promotions.preview is not None
    assert promotions.preview.sender == "deals@shop.example"
    assert promotions.preview.subject == "50% off everything"


async def test_primary_is_probed_with_the_category_query_not_a_label_id() -> None:
    """Primary = inbox-minus-categorized, which only ``category:primary`` expresses (#765)."""
    provider = _provider()
    client = _mock_client()
    calls: list[tuple[str, Any]] = []
    _wire(
        client,
        newest={"primary": "tp", "promotions": "tm"},
        threads={"tp": _thread("Lunch?", "a@x.com"), "tm": _thread("Sale", "b@x.com")},
        unread={"CATEGORY_PERSONAL": 1, "CATEGORY_PROMOTIONS": 2},
        calls=calls,
    )
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    await provider.list_categories(label="INBOX")

    probes = [params for url, params in calls if url == "/users/me/threads"]
    assert {"labelIds": "INBOX", "q": "category:primary", "maxResults": 1} in probes
    # Every probe is scoped to the folder as well as the category — a tab lists Inbox mail.
    assert all(p["labelIds"] == "INBOX" and p["maxResults"] == 1 for p in probes)


async def test_an_empty_category_is_hidden_the_way_gmail_hides_forums() -> None:
    provider = _provider()
    client = _mock_client()
    _wire(
        client,
        newest={"primary": "tp", "promotions": "tm", "social": None, "updates": None},
        threads={"tp": _thread("Lunch?", "a@x.com"), "tm": _thread("Sale", "b@x.com")},
        unread={"CATEGORY_PERSONAL": 1, "CATEGORY_PROMOTIONS": 9},
    )
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    tabs = await provider.list_categories(label="INBOX")

    assert [t.id for t in tabs] == ["primary", "promotions"]  # forums/social/updates all empty


async def test_primary_renders_even_when_it_is_the_empty_one() -> None:
    """Primary is the fallback bucket: an inbox with none of it still has a Primary tab."""
    provider = _provider()
    client = _mock_client()
    _wire(
        client,
        newest={"primary": None, "promotions": "tm"},
        threads={"tm": _thread("Sale", "b@x.com")},
        unread={"CATEGORY_PROMOTIONS": 9},
    )
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    tabs = await provider.list_categories(label="INBOX")

    assert [t.id for t in tabs] == ["primary", "promotions"]
    assert tabs[0].preview is None  # no mail → no preview line
    assert tabs[0].unread is None  # the label 404s → no badge, not a forced zero (ADR-0030)


async def test_an_uncategorized_mailbox_gets_no_tab_strip_at_all() -> None:
    """Nothing outside Primary means Gmail's tabs are off — one lone tab would be noise."""
    provider = _provider()
    client = _mock_client()
    _wire(client, newest={"primary": "tp"}, threads={"tp": _thread("Lunch?", "a@x.com")})
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    assert await provider.list_categories(label="INBOX") == []


async def test_no_enrichment_happens_when_the_mailbox_isnt_categorized() -> None:
    """The bail-out is before the expensive wave: probes only, no gets, no label counts."""
    provider = _provider()
    client = _mock_client()
    calls: list[tuple[str, Any]] = []
    _wire(
        client,
        newest={"primary": "tp"},
        threads={"tp": _thread("Lunch?", "a@x.com")},
        calls=calls,
    )
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    await provider.list_categories(label="INBOX")

    assert {url for url, _ in calls} == {"/users/me/threads"}


async def test_a_preview_fetch_failure_costs_that_tab_its_preview_not_the_strip() -> None:
    """Best-effort enrichment: a thread deleted between the probe and the get is survivable."""
    provider = _provider()
    client = _mock_client()

    async def fake_get(url: str, **kwargs: Any) -> MagicMock:
        params = kwargs.get("params") or {}
        if url == "/users/me/threads":
            category = str(params["q"]).removeprefix("category:")
            return _resp({"threads": [{"id": f"t-{category}"}]} if category != "forums" else {})
        if url == "/users/me/threads/t-promotions":
            raise httpx.HTTPError("gone")
        if url.startswith("/users/me/threads/"):
            return _resp(_thread("Something", "someone@example.com"))
        if url.startswith("/users/me/labels/"):
            return _resp({"messagesUnread": 4})
        raise AssertionError(f"unexpected GET {url}")

    client.get = AsyncMock(side_effect=fake_get)
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    tabs = await provider.list_categories(label="INBOX")

    by_id = {t.id: t for t in tabs}
    assert set(by_id) == {"primary", "promotions", "social", "updates"}
    assert by_id["promotions"].preview is None  # lost its preview…
    assert by_id["promotions"].unread == 4  # …but keeps its badge, and the strip is intact
    assert by_id["social"].preview is not None


async def test_an_uncountable_category_shows_no_badge_rather_than_zero() -> None:
    """A ``labels.get`` failure leaves ``unread`` None — a capability gate (ADR-0030)."""
    provider = _provider()
    client = _mock_client()
    _wire(
        client,
        newest={"primary": "tp", "updates": "tu"},
        threads={"tp": _thread("Lunch?", "a@x.com"), "tu": _thread("Receipt", "b@x.com")},
        unread={"CATEGORY_PERSONAL": 5},  # CATEGORY_UPDATES deliberately absent → raises
    )
    provider._make_client = MagicMock(return_value=client)  # type: ignore[method-assign]

    tabs = await provider.list_categories(label="INBOX")

    by_id = {t.id: t for t in tabs}
    assert by_id["primary"].unread == 5
    assert by_id["updates"].unread is None
