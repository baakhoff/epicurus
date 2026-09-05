"""Mail module — MCP tool surface (ADR-0016).

Provider-agnostic tools: ``mail_search``, ``mail_read``, ``mail_send``, ``mail_reply``,
and the read-state pair ``mail_mark_read`` / ``mail_mark_unread``.
The tool names and signatures are domain-neutral; no Gmail specifics appear in the
tool surface (the manifest declares Gmail's OAuth scopes for the connect flow, #241).
``mail_search`` returns a :func:`~epicurus_core.tool_envelope` so the UI renders
each result as an entity-reference chip (ADR-0019): hover for the hover-card,
click to open the full message in the right-panel email-reader.
``mail_send`` and ``mail_reply`` are **draft-first** (ADR-0085, #563): they *compose* a
message and return a :func:`~epicurus_core.draft_review` envelope — they never transmit.
The core suspends the turn and shows the draft in a split-pane; only the operator's Confirm
triggers the actual send (the module's ``POST /send`` endpoint, invoked by the core). The MCP
surface therefore exposes **no** tool that sends, so the agent cannot deliver mail on its own.
``mail_reply`` (#461) composes a reply that stays in the original conversation thread —
RFC-2822 ``In-Reply-To``/``References`` plus the provider's native thread association —
deriving the recipient and subject from the original message rather than taking them as
arguments.
``mail_mark_read`` / ``mail_mark_unread`` flip a message's read state; the
``email-reader`` panel also surfaces them as a tool-backed toggle (ADR-0024).
``mail_search`` additionally takes a ``category`` (#765) — the same Primary / Promotions /
Social / Updates / Forums buckets the mail page renders as tabs over the Inbox — so a chat
turn can ask about one of them without knowing any provider query syntax.
Mail is provider-only: with **no Google account connected** (#764) every tool answers with
:data:`_NOT_CONNECTED_HINT` — one model-actionable sentence naming both ways out — instead of
raising, and the page falls back to :func:`mailbox_disconnected`.
When the module cannot even *ask* whether an account is connected (#835) that is a third,
separate answer: :func:`unreachable_hint` and :func:`mailbox_unreachable` say the core is
unreachable and that nothing needs reconnecting, rather than repeating the not-connected
wording and sending an operator to fix an account that was never broken.
"""

from __future__ import annotations

from typing import Any

import httpx

from epicurus_core import (
    AutomationTemplate,
    EntityRef,
    EpicurusModule,
    PageSpec,
    UiAction,
    UiSection,
    capped_listing,
    draft_review,
    event_subject,
    tool_envelope,
)
from epicurus_mail.cache import CachedMailbox
from epicurus_mail.gmail import GMAIL_API_SCOPES
from epicurus_mail.provider import (
    ComposedMessage,
    MailCategory,
    MailMessage,
    MailNotConnected,
    MailProvider,
)

MODULE_NAME = "mail"
MAILBOX_PAGE_ID = "mailbox"
# The default folder the page opens on, and the Gmail label the nav-badge unread reflects.
DEFAULT_LABEL = "INBOX"
# The kind every mail entity-reference / attachment carries (ADR-0019).
MESSAGE_KIND = "message"
# Cap on threads per list page (ADR-0087). Each thread costs a metadata fetch, so a bounded
# page keeps one request from fanning out across an unbounded mailbox (#539); the shell pages
# further with the returned cursor.
MAILBOX_PAGE_SIZE = 25

# Shown by every tool when no Google account is connected at all (#764) — the state a fresh
# self-host starts in and the one a Settings → Disconnect leaves behind. Distinct from the
# scope hints below, which all presuppose a *connected* account missing one permission:
# telling an operator with no connection to "reconnect to grant a permission" sends them
# looking for a setting that isn't there. Model-actionable in the same shape as those hints
# — a plain sentence naming the cause and both ways out — so the agent can relay it verbatim
# instead of surfacing a provider traceback. Mail is Gmail-only (ADR-0032: no collections, no
# local provider), so "disable the module" is a legitimate second answer, not a dismissal.
_NOT_CONNECTED_HINT = (
    "Google is not connected, so there is no mailbox to read or send from. Connect it in"
    " Settings → Connected accounts, or disable the mail module if you don't use Gmail."
)

# Shown when the module cannot reach the core to find out whether an account is connected
# (#835) — the third availability state. Everything about this hint is the *opposite* of
# _NOT_CONNECTED_HINT's advice, which is exactly why the two must never be interchanged: the
# account is probably fine, there is nothing to reconnect and nothing to disable, and the only
# useful action is to wait. Say so, name the reason, and do not offer a Settings trip that
# would waste the operator's time and might disconnect a working account.
_UNREACHABLE_HINT = (
    "Couldn't tell whether Google is connected — {reason}. That's a problem reaching the core,"
    " not a missing account, so nothing needs reconnecting; the mailbox should come back on"
    " its own once the core answers again."
)

# The reason clause used when a provider reports ``unreachable`` without one. A provider is
# supposed to supply it, but a hint reading "— reason unknown" is still a truthful sentence,
# where a bare "— ." is a bug on screen.
_UNREACHABLE_FALLBACK_REASON = "the reason wasn't reported"

# Shown when ``messages.modify`` is rejected for lack of scope — the operator connected
# Google before mail required ``gmail.modify`` and must reconnect to grant it.
_SCOPE_HINT = (
    "Couldn't change the read state: the connected Google account is missing the Gmail"
    " modify permission. Reconnect Google (Settings → Connect) to grant it."
)

# Shown when ``messages.send`` is rejected for lack of scope (#513) — mirrors _SCOPE_HINT's
# reconnect-hint treatment for send/reply instead of a bare exception.
_SCOPE_HINT_SEND = (
    "Couldn't send: the connected Google account is missing the Gmail send permission."
    " Reconnect Google (Settings → Connect) to grant it."
)

# Shown when archive/trash (``messages.modify`` / ``messages.trash``) is rejected for lack of
# scope (ADR-0087). Both need ``gmail.modify`` (already granted for mark read/unread), so a
# bare 403 there means the operator connected before mail required it and must reconnect.
_SCOPE_HINT_TRIAGE = (
    "Couldn't move the message: the connected Google account is missing the Gmail modify"
    " permission. Reconnect Google (Settings → Connect) to grant it."
)

# Shown when mail_reply's own metadata lookup (the original message's Reply-To/From/
# Subject/Message-ID — needs gmail.modify) is rejected for lack of scope. Distinct wording
# from _SCOPE_HINT: that constant talks about "the read state", which doesn't apply here —
# the reply was never composed, let alone sent (#538).
_SCOPE_HINT_REPLY_LOOKUP = (
    "Couldn't reply: the connected Google account is missing the Gmail modify permission"
    " needed to look up the original message. Reconnect Google (Settings → Connect) to"
    " grant it."
)

# Shown when a read (``mail_search`` / ``mail_read``) is rejected for lack of scope — mail
# needs ``gmail.modify`` (which covers reads); a bare 403 there means the operator hasn't
# granted it. Read-context wording, distinct from the modify/send/reply hints above.
_SCOPE_HINT_READ = (
    "Couldn't reach Gmail: the connected Google account is missing the Gmail permission this"
    " needs. Reconnect Google (Settings → Connect) to grant it."
)


def unreachable_hint(reason: str | None) -> str:
    """The operator/model-facing sentence for the ``unreachable`` availability state (#835)."""
    return _UNREACHABLE_HINT.format(reason=reason or _UNREACHABLE_FALLBACK_REASON)


# Gmail returns 403 both for a missing OAuth scope and for per-user/per-day rate limiting
# (``usageLimits``) — the reasons below are Google's **legacy** Discovery-API error codes for
# the latter. Blaming every 403 on a missing scope misdirects an operator who is simply being
# throttled (#538). Google's newer APIs report the same thing in the AIP-193 shape instead
# (``error.status == "RESOURCE_EXHAUSTED"`` / ``error.details[].reason == "RATE_LIMIT_EXCEEDED"``);
# :func:`_is_rate_limited` checks both so a shape migration doesn't silently misfire (#557).
_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}
)
_AIP_RATE_LIMIT_STATUS = "RESOURCE_EXHAUSTED"
_AIP_RATE_LIMIT_REASON = "RATE_LIMIT_EXCEEDED"

_RATE_LIMIT_HINT = (
    "Gmail is rate-limiting this account (too many requests in a short time, or the daily"
    " quota was reached). Wait a bit and try again."
)


def _is_rate_limited(error: dict[str, Any]) -> bool:
    """Whether a Gmail error body names a rate-limit cause, in either shape (#538, #557).

    Legacy (Gmail v1 today): ``error.errors[].reason`` ∈ :data:`_RATE_LIMIT_REASONS`. Modern
    AIP-193 (if Gmail ever migrates): ``error.status == "RESOURCE_EXHAUSTED"`` or an
    ``error.details[]`` entry whose ``reason`` is ``"RATE_LIMIT_EXCEEDED"``. Every field access
    is defensive — a non-string ``reason`` (an otherwise well-formed body with a nested object
    there) is skipped, not fed to the ``in`` membership test where it would raise ``TypeError``
    on an unhashable value instead of falling back to the scope hint.
    """
    if error.get("status") == _AIP_RATE_LIMIT_STATUS:
        return True
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason") == _AIP_RATE_LIMIT_REASON:
                return True
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                reason = item.get("reason")
                if isinstance(reason, str) and reason in _RATE_LIMIT_REASONS:
                    return True
    return False


def _rate_limit_hint(response: httpx.Response) -> str:
    """The rate-limit hint, extended with Gmail's ``Retry-After`` when it sends one (#557).

    ``Retry-After`` is either a number of seconds or an HTTP-date; surface whichever it is so
    the operator knows how long to wait rather than guessing.
    """
    retry_after = (response.headers.get("Retry-After") or "").strip()
    if not retry_after:
        return _RATE_LIMIT_HINT
    if retry_after.isdigit():
        return f"{_RATE_LIMIT_HINT} Gmail suggests waiting about {retry_after} seconds."
    return f"{_RATE_LIMIT_HINT} Gmail asks you to retry after {retry_after}."


def _describe_403(exc: httpx.HTTPStatusError, scope_hint: str) -> str:
    """The user-facing hint for a Gmail 403: *scope_hint* unless the error body names a
    rate-limit cause (#538) — recognized in either the legacy or the AIP-193 shape (#557) — in
    which case that's the real cause. Falls back to *scope_hint* whenever the body doesn't parse
    into Google's error shape, since a missing scope is the far more common cause of an
    unparseable 403.
    """
    try:
        error = exc.response.json()["error"]
    except (ValueError, KeyError, TypeError):
        return scope_hint
    if isinstance(error, dict) and _is_rate_limited(error):
        return _rate_limit_hint(exc.response)
    return scope_hint


def _describe_gmail_error(exc: httpx.HTTPStatusError, scope_hint: str) -> str | None:
    """A hint a tool can return in place of a raw exception for a Gmail HTTP error, or ``None``
    to signal "not one we soften — re-raise" (#538, #557).

    A **429** (Too Many Requests) is unambiguously rate limiting → the wait-and-retry hint
    (honoring ``Retry-After``). A **403** may be a missing scope *or* a rate limit disguised as
    ``usageLimits`` → :func:`_describe_403` disambiguates. Any other status returns ``None``.
    """
    status = exc.response.status_code
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return _rate_limit_hint(exc.response)
    if status == httpx.codes.FORBIDDEN:
        return _describe_403(exc, scope_hint)
    return None


def _draft_summary(message: ComposedMessage) -> str:
    """A one-line label for a composed draft — shown in the turn activity and logs (ADR-0085)."""
    return f"Email to {message.to} — {message.subject or '(no subject)'}"


def build_module(provider: MailProvider) -> EpicurusModule:
    """Build the mail module and register its MCP tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.21.0",
        description=(
            "Provider-agnostic mail — search, read, and draft-first send/reply. Gmail is the v0.1"
            " provider."
        ),
        # Tenant data portability (#867/#874): every table this module persists is a
        # Gmail-derived cache (see epicurus_mail.portability), so there is nothing here
        # worth carrying — deliberately `False`, per the documented convention
        # (docs/reference/modules.md: a module with nothing to carry leaves the flag off
        # and the archive records it as excluded) rather than listing an always-empty
        # component the operator has to read past on every export. The routes are still
        # wired below ("serving the routes while saying not yet" is the documented
        # in-between state), so a future genuine preference is a one-line flip.
        portable=False,
        ui=UiSection(
            icon="mail",
            summary=(
                "Lets the agent search, read, and compose mail via your connected Google account."
                " The agent never sends on its own — it composes a draft you Confirm or Decline"
                " (ADR-0085). Requires Gmail scopes on the Google OAuth connection."
            ),
            status_url="/status",
            # Draft-first (ADR-0085): these compose a draft the assistant shows for your review;
            # they are no longer one-tap sends, so they are not danger actions (nothing is
            # delivered until you Confirm the draft). The real send is the ``POST /send`` endpoint.
            actions=[
                UiAction(
                    tool="mail_send",
                    label="Compose mail",
                    description=(
                        "Compose a message. The assistant shows it for your review — nothing is"
                        " sent until you confirm the draft."
                    ),
                ),
                UiAction(
                    tool="mail_reply",
                    label="Compose reply",
                    description=(
                        "Compose a reply in the same conversation thread. Shown for your review;"
                        " nothing is sent until you confirm the draft."
                    ),
                ),
            ],
        ),
        # A left-nav Mail page (ADR-0087): the module supplies labels/threads/messages and
        # the core shell renders the `mailbox` client (rail -> thread list -> conversation +
        # compose/reply). No module markup. Reads flow through the generic page proxy; the
        # send + attachment endpoints are gated, mailbox-only core proxies.
        pages=[
            PageSpec(
                id=MAILBOX_PAGE_ID,
                title="Mail",
                archetype="mailbox",
                icon="mail",
                nav_order=50,
            )
        ],
        resolver=True,
        # The Gmail API scopes the shell requests when connecting Google (#241); the core
        # adds the default identity scopes. Without these, Gmail API calls return 403.
        oauth_scopes={"google": GMAIL_API_SCOPES},
        # Starter presets for the Templates tab (#705, ADR-0105) — never auto-instantiated.
        automation_templates=[
            AutomationTemplate(
                key="on-mail-received",
                name="Triage new mail",
                description=(
                    "Runs a read-only turn whenever a new message arrives, so you get a"
                    " quick sense of what it's about without opening your inbox."
                ),
                trigger={"module": MODULE_NAME, "event_type": "mail.received"},
                prompt=(
                    "A new email just arrived. Search for it and read it if you need to, then"
                    " summarize it in one or two sentences, noting whether it looks important"
                    " or time-sensitive."
                ),
                autonomy="notify",
                sinks=["push"],
            ),
            AutomationTemplate(
                key="morning-unread-digest",
                name="Morning unread digest",
                description="A short daily summary of what's sitting unread in your inbox.",
                trigger={"cadence": "daily", "hour": 8},
                prompt=(
                    "Search your unread mail and give a short digest of what's new, grouped by"
                    " sender or topic. Skip anything that looks like a pure notification/no-reply."
                ),
                autonomy="notify",
                sinks=["push"],
            ),
        ],
    )

    module.emits(event_subject("mail.sent"), "Emitted after a message is sent successfully.")
    module.emits(
        event_subject("mail.received"),
        "Emitted per genuinely-new message during an incremental sync — never on an "
        "initial or full resync (#663).",
    )
    module.emits(
        event_subject("mail.sync_failed"),
        "Emitted when a reconcile fails (provider/auth error, or an expired sync cursor "
        "forcing a full resync) — rate-limited per account.",
    )

    @module.tool(side_effect="read")
    async def mail_search(query: str, max_results: int = 10, category: str | None = None) -> str:
        """Search for mail matching *query*.

        Supports the same query syntax as Gmail (e.g. ``from:alice``,
        ``subject:invoice``, ``is:unread``).  Returns up to *max_results*
        messages as entity-reference chips — hover for a quick preview, click
        to open the full message in the panel.  Each chip carries the message
        id; call ``mail_read`` explicitly only when you need the body as text.

        Pass *category* to restrict the search to one of the inbox categories the mail page
        shows as tabs — so "summarize today's Promotions" is
        ``mail_search(query="newer_than:1d", category="promotions")``. Combine it freely with
        the rest of the query (``is:unread``, ``from:``, a date filter…); the two are ANDed.

        Args:
            query: Mail search expression (Gmail query syntax). May be empty when *category*
                alone is the filter.
            max_results: Maximum number of messages to return (1-50, default 10).
            category: Optional inbox category — ``primary`` (mail not in any other category),
                ``promotions``, ``social``, ``updates``, or ``forums``. Omit to search
                everything.
        """
        capped = max(1, min(max_results, 50))
        # A category is a provider capability, so the id -> query translation lives behind the
        # seam (never a Gmail operator hardcoded here); an unsupported one is reported rather
        # than silently dropped, which would hand back "all mail" for a narrowed request.
        if category:
            scoped = provider.category_query(category.strip().lower())
            if scoped is None:
                return (
                    f"Can't filter by category {category!r}: this mail account doesn't support"
                    " categories, or that isn't one of primary, promotions, social, updates,"
                    " forums."
                )
            query = f"{scoped} {query}".strip()
        try:
            messages = await provider.search(query, capped)
        except MailNotConnected:
            return _NOT_CONNECTED_HINT
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_READ)
            if hint is not None:
                return hint  # a rate-limit (429/403) or missing-scope hint, not a raw traceback
            raise
        if not messages:
            return tool_envelope("No messages found.", [])
        refs = [
            EntityRef(
                ref_id=m.id,
                module=MODULE_NAME,
                kind="message",
                title=m.subject or "(no subject)",
                summary=m.snippet,
            )
            for m in messages
        ]
        lines = [
            f"- [{m.subject or '(no subject)'}] from {m.sender}"
            + (f" ({m.date})" if m.date else "")
            for m in messages
        ]
        # Capped the same way as the entity-ref id block the core appends (both default to
        # LIST_CAP, #468/#522) — max_results is already clamped to 50 above so this can't
        # bite today, but it keeps mail_search consistent with calendar's adoption (#539)
        # rather than reinventing the listing text.
        text = capped_listing(lines, noun="message")
        return tool_envelope(text, refs)

    @module.tool(side_effect="read")
    async def mail_read(message_id: str) -> str:
        """Fetch the full content of a mail message by its *message_id*.

        Returns the message subject, sender, date, and decoded plain-text body
        as a readable block.  Use ``mail_search`` first to discover message IDs.
        The UI opens the full message in the right-panel when a user clicks an
        email chip — call this tool only when you need the body as text for
        reasoning or quoting.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            m = await provider.read(message_id)
        except MailNotConnected:
            return _NOT_CONNECTED_HINT
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_READ)
            if hint is not None:
                return hint
            raise
        parts = [f"Subject: {m.subject or '(no subject)'}"]
        parts.append(f"From: {m.sender}")
        if m.date:
            parts.append(f"Date: {m.date}")
        parts.append("")
        parts.append(m.body or "(no body)")
        return "\n".join(parts)

    # side_effect="propose" (#721, ADR-0112): unconditionally draft-first, never a direct send —
    # the automations dial can hand this to a propose-autonomy turn with no caveat.
    @module.tool(side_effect="propose")
    async def mail_send(to: str, subject: str, body: str) -> str:
        """Compose an email for the user to review — this does **not** send it (ADR-0085).

        There is no tool that sends. You compose the message; the user reviews it in a split-pane
        and presses **Confirm** to send or **Decline** to drop it. Compose freely — the user is
        the send button. On Decline you are told (with any reason they give) so you can revise and
        compose again.

        Args:
            to: Recipient email address.
            subject: Message subject line.
            body: Plain-text message body.

        Pauses the turn to show the draft; the turn resumes once the user confirms or declines.
        """
        recipient = to.strip()
        if not recipient:
            return "error: a recipient (`to`) is required to compose a message."
        # The only tool that reaches no provider at all — composing is pure local work, so
        # without this it would happily hand back a draft that can never be delivered, and
        # the operator would discover the missing connection at Confirm time (#764). Cheap
        # credential probe (#209), not a live Gmail call; refusing here keeps the draft-first
        # split-pane from ever opening on a message with nowhere to go. Both non-connected
        # states refuse, but with their own wording (#835): a draft is equally undeliverable
        # either way, while the advice ("connect Google" vs "wait, nothing is broken") is not.
        availability = await provider.availability()
        if availability.state == "not_connected":
            return _NOT_CONNECTED_HINT
        if availability.state == "unreachable":
            return unreachable_hint(availability.reason)
        message = ComposedMessage(to=recipient, subject=subject, body=body)
        return draft_review(
            kind="mail",
            module=MODULE_NAME,
            summary=_draft_summary(message),
            draft=message.model_dump(),
        )

    # side_effect="propose" (#721, ADR-0112): same unconditional draft-first guarantee as
    # mail_send.
    @module.tool(side_effect="propose")
    async def mail_reply(message_id: str, body: str) -> str:
        """Compose a reply for the user to review — this does **not** send it (ADR-0085).

        Like ``mail_send`` this composes only. The reply stays in the original conversation
        thread; the recipient and subject are derived from the original message (preferring its
        ``Reply-To`` over its sender when it carries one, and its subject prefixed with "Re:"
        unless already a reply), so you supply just the body. The user reviews the draft in a
        split-pane and **Confirm**s or **Decline**s — no tool sends; the user is the send button.
        The reply body is sent **clean**: it is not auto-quoted with the original message's text.

        Args:
            message_id: The message being replied to (from ``mail_search`` or ``mail_read``).
            body: Plain-text reply body.

        Pauses the turn to show the draft; the turn resumes once the user confirms or declines.
        """
        try:
            message = await provider.compose_reply(message_id, body)
        except MailNotConnected:
            return _NOT_CONNECTED_HINT
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_REPLY_LOOKUP)
            if hint is not None:
                return hint
            raise
        return draft_review(
            kind="mail",
            module=MODULE_NAME,
            summary=_draft_summary(message),
            draft=message.model_dump(),
        )

    @module.tool()
    async def mail_mark_read(message_id: str) -> str:
        """Mark a mail message as read.

        Clears the unread flag on the message identified by *message_id* (discover ids
        with ``mail_search``). Distinct from ``mail_read``, which fetches the body —
        this only changes read state and returns nothing to read. Idempotent.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.set_unread(message_id, unread=False)
        except MailNotConnected:
            return _NOT_CONNECTED_HINT
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT)
            if hint is not None:
                return hint
            raise
        return f"marked-read:{message_id}"

    @module.tool()
    async def mail_mark_unread(message_id: str) -> str:
        """Mark a mail message as unread.

        Restores the unread flag on the message identified by *message_id* (discover ids
        with ``mail_search``). Idempotent.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.set_unread(message_id, unread=True)
        except MailNotConnected:
            return _NOT_CONNECTED_HINT
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT)
            if hint is not None:
                return hint
            raise
        return f"marked-unread:{message_id}"

    @module.tool()
    async def mail_archive(message_id: str) -> str:
        """Archive a mail message — remove it from the Inbox without deleting it.

        The message stays in All Mail and is fully recoverable; this only takes it out of
        the Inbox (discover ids with ``mail_search``). Idempotent — archiving an
        already-archived message is a no-op.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.archive(message_id)
        except MailNotConnected:
            return _NOT_CONNECTED_HINT
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_TRIAGE)
            if hint is not None:
                return hint
            raise
        return f"archived:{message_id}"

    @module.tool()
    async def mail_trash(message_id: str) -> str:
        """Move a mail message to Trash — recoverable, not a permanent delete.

        The message goes to Trash (auto-purged by the provider after its retention window)
        and can be restored until then; this is **not** a permanent delete. Discover ids
        with ``mail_search``. Idempotent.

        Args:
            message_id: The message ID returned by ``mail_search``.
        """
        try:
            await provider.trash(message_id)
        except MailNotConnected:
            return _NOT_CONNECTED_HINT
        except httpx.HTTPStatusError as exc:
            hint = _describe_gmail_error(exc, _SCOPE_HINT_TRIAGE)
            if hint is not None:
                return hint
            raise
        return f"trashed:{message_id}"

    return module


# ── mailbox page data (ADR-0087) ──────────────────────────────────────────────
# Pure builders the app's page routes call; unit-testable against a mocked provider.
# Every mutation is a `BoardAction` (ADR-0024) naming an existing MCP tool, so the page
# mutates through the same validated tool proxy the agent uses — no module markup.


def _mark_read_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` (ADR-0024) that marks an unread message read."""
    return {
        "tool": "mail_mark_read",
        "label": "Mark as read",
        "intent": "default",
        "icon": "check",
        "args": {"message_id": message_id},
    }


def _mark_unread_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` (ADR-0024) that marks a read message unread."""
    return {
        "tool": "mail_mark_unread",
        "label": "Mark as unread",
        "intent": "default",
        "icon": "mail",
        "args": {"message_id": message_id},
    }


def _archive_action(message_id: str) -> dict[str, Any]:
    """A `BoardAction` that archives a message out of the Inbox (ADR-0087)."""
    return {
        "tool": "mail_archive",
        "label": "Archive",
        "intent": "default",
        "icon": "archive",
        "args": {"message_id": message_id},
    }


def _trash_action(message_id: str) -> dict[str, Any]:
    """A danger `BoardAction` that moves a message to Trash (ADR-0087).

    A danger action must carry a confirm prompt (the shared BoardAction contract), so the
    shell gates it behind a dialog — trash is recoverable but still a triage step the
    operator should mean.
    """
    return {
        "tool": "mail_trash",
        "label": "Trash",
        "intent": "danger",
        "icon": "trash",
        "args": {"message_id": message_id},
        "confirm": "Move this message to Trash?",
    }


def message_payload(message: MailMessage) -> dict[str, Any]:
    """One thread-pane message as the shared `email-reader` shape + attachments (ADR-0087).

    The same envelope the panel's `GET /messages/{id}` returns (ADR-0024), so the page's
    thread pane and the panel reader render through one component — plus this message's
    attachments and its full triage action set (mark toggle, Archive, Trash). The toggle
    flips to whichever state the message is *not* in.
    """
    toggle = _mark_read_action(message.id) if message.unread else _mark_unread_action(message.id)
    return {
        "subject": message.subject or "(no subject)",
        "from": message.sender,
        "date": message.date,
        "body": message.body or "",
        # The HTML body (ADR-0097, #627) — the shell renders it in a sandboxed iframe with
        # inline ``cid:`` images resolved through the module and remote images blocked by
        # default; ``body`` (text) stays the fallback for a text-only message.
        "body_html": message.body_html,
        "module": MODULE_NAME,
        "message_id": message.id,
        "unread": message.unread,
        "attachments": [att.model_dump() for att in message.attachments],
        "actions": [toggle, _archive_action(message.id), _trash_action(message.id)],
    }


def tab_payload(category: MailCategory) -> dict[str, Any]:
    """One category tab as the page's `tabs` entry (#765).

    Hand-built rather than ``model_dump()``d so the wire key is ``from`` (a Python keyword,
    hence ``sender`` on the model) — the same mapping :func:`message_payload` already makes
    for a message's sender, so the shell sees one spelling everywhere.
    """
    preview = category.preview
    return {
        "id": category.id,
        "title": category.title,
        "unread": category.unread,
        "preview": (
            {"from": preview.sender, "subject": preview.subject} if preview is not None else None
        ),
    }


def _resolve_tab(
    provider: MailProvider, tabs: list[MailCategory], requested: str | None
) -> tuple[str, str | None]:
    """Resolve a ``?tab=`` selection to ``(echoed id, provider query)`` (#765).

    Yields ``("", None)`` — i.e. "no tab selected, list the whole Inbox" — for an absent
    selection, one that isn't among the tabs actually offered, or one the provider can't
    express as a query. So a hand-crafted ``?tab=nonsense`` degrades to the plain Inbox
    rather than scoping the list by something the provider would misread, and the echoed
    ``active_tab`` never claims a tab the shell isn't showing.
    """
    if not requested or all(tab.id != requested for tab in tabs):
        return "", None
    scoped = provider.category_query(requested)
    return (requested, scoped) if scoped else ("", None)


def mailbox_disconnected(label: str | None = None) -> dict[str, Any]:
    """The `mailbox` list payload for "no Google account is connected" (#764).

    Structurally a valid, *empty* list read — no rail, no tabs, no threads — plus the
    ``disconnected`` flag the shell keys its honest empty state off. Deliberately **not** an
    HTTP error: opening Mail on a self-host that has never connected Google is a normal
    first-run state, and a page that errors on plain navigation tells the operator they broke
    something rather than that there is a switch to flip. The flag is the whole contract; a
    shell that ignores it renders the ordinary "this folder is empty" view, which is wrong but
    not broken.

    The local cache is intentionally *not* cleared and not served: the rows stay on disk, so a
    reconnect restores the mailbox instantly (no resync, no restart), while a disconnected
    module shows nothing it no longer has permission to show.
    """
    return {**_empty_mailbox(label), "disconnected": True}


def mailbox_unreachable(label: str | None = None, reason: str | None = None) -> dict[str, Any]:
    """The `mailbox` list payload for "we couldn't find out whether Google is connected" (#835).

    The same structurally-valid empty list as :func:`mailbox_disconnected`, under a **different
    flag**, because it wants the opposite empty state. ``disconnected: true`` makes the shell
    say "Google is not connected — connect it in Settings"; saying that when the truth is that
    the core didn't answer sends the operator to reconnect a working account, and a disconnect/
    reconnect round trip is not a harmless thing to talk someone into. So this carries
    ``unreachable`` — a non-empty reason clause, present only in this state — and never
    ``disconnected``.

    A shell that only knows about ``disconnected`` degrades to the ordinary "this folder is
    empty" view: wrong, but wrong in the harmless direction, and it never puts the
    not-connected words on screen. Deliberately not an HTTP error, for the same reason
    :func:`mailbox_disconnected` isn't: an unreachable core is transient, and the page should
    recover on its next poll rather than making plain navigation look broken.
    """
    return {**_empty_mailbox(label), "unreachable": reason or _UNREACHABLE_FALLBACK_REASON}


def _empty_mailbox(label: str | None) -> dict[str, Any]:
    """A structurally-valid, *empty* `mailbox` list read — no rail, no tabs, no threads.

    The shared body of the two no-mailbox payloads above; each adds its own single flag. The
    connected path never routes through here, and neither flag appears on it, so a working
    mailbox carries no absence keys at all.
    """
    return {
        "title": "Mail",
        "labels": [],
        "active_label": label or DEFAULT_LABEL,
        "query": "",
        "tabs": [],
        "active_tab": "",
        "threads": [],
        "next_cursor": None,
    }


async def build_mailbox_list(
    provider: MailProvider,
    *,
    mailbox: CachedMailbox | None = None,
    label: str | None = None,
    query: str | None = None,
    tab: str | None = None,
    cursor: str | None = None,
    reconcile: bool = False,
    limit: int = MAILBOX_PAGE_SIZE,
) -> dict[str, Any]:
    """The `mailbox` list read (ADR-0087): the rail, the Inbox tabs, and one page of threads.

    Browsing is folder-scoped (the active *label*); a *query* searches the whole mailbox
    (Gmail syntax, like ``mail_search``) while the rail keeps highlighting the current
    folder. Unread counts are filled only for Inbox (the nav-badge source) and the active
    label to bound the rail's provider calls. ``limit`` is clamped to the page cap so one
    fetch can't scan an unbounded mailbox (#539); the shell pages on with ``next_cursor``.

    Cache-first landing (ADR-0096, #623): when a *mailbox* orchestrator is supplied and this
    is the plain landing view (no *query*, no *tab*, first page), it serves from the local
    cache instantly — ``reconcile=True`` first pulls the provider delta into the cache. Search,
    a tab-scoped list, and deeper (*cursor*) pages bypass the cache and read the provider live,
    since the cache only materializes the default landing page.

    **Category tabs (#765).** When the rail selection is the Inbox and nothing is being
    searched, the payload gains ``tabs`` — Gmail-style Primary / Promotions / Social / Updates
    (+ Forums when it has mail), each with its unread count and a one-line preview of its
    newest message — plus ``active_tab``. A *tab* selection scopes the thread list through the
    provider's own query mechanism, the same one the rail and search already use, so Primary
    correctly means inbox-minus-categorized. Every other rail selection, and any search, omits
    the block entirely; so does a provider that doesn't classify mail — and a payload without
    ``tabs`` renders exactly the pre-tabs page.

    **Disconnected (#764).** With no Google account connected this short-circuits to
    :func:`mailbox_disconnected` — an empty list carrying ``disconnected: true`` — before
    either read path runs, so the page states the honest reason instead of erroring or
    serving cached rows the module can no longer refresh.

    **Unreachable (#835).** When the availability probe can't reach the core at all it
    short-circuits to :func:`mailbox_unreachable` instead — the same empty list, carrying
    ``unreachable: "<reason>"``. Same short-circuit, deliberately different flag: the page must
    not tell an operator to reconnect Google because the core happened to be restarting.

    Args:
        provider: The active mail backend.
        mailbox: The cache orchestrator for the landing fast path (``None`` → always live).
        label: The rail selection; defaults to the Inbox.
        query: Optional provider-native search; searches all mail when present.
        tab: Optional category tab id scoping the list within the Inbox (#765).
        cursor: Opaque next-page token from a previous read (``None`` for the first page).
        reconcile: On the cached landing path, pull the provider delta before serving.
        limit: Requested page size, clamped to :data:`MAILBOX_PAGE_SIZE`.
    """
    active = label or DEFAULT_LABEL
    # Reflect the *connection*, not the cache (#764). The cached landing path below never
    # touches the provider, so without this gate a disconnected mailbox would keep serving
    # the rows it synced while it was connected — the page would look fine and be a lie.
    # One cheap credential probe (#209, the same call `/status` and the poller make), placed
    # ahead of both paths so the live path gets the honest answer too rather than a raw 404
    # from a token fetch mid-fan-out. Both no-mailbox states stop here, under their own flag
    # (#835): the gate answering "no" is not the same fact as the gate being unable to answer,
    # and the empty state each wants is the other one's bad advice.
    availability = await provider.availability()
    if availability.state == "not_connected":
        return mailbox_disconnected(active)
    if availability.state == "unreachable":
        return mailbox_unreachable(active, availability.reason)
    capped = max(1, min(limit, MAILBOX_PAGE_SIZE))
    q = (query or "").strip() or None
    # Tabs sit over the Inbox only, and never over a search (a search spans every folder, so
    # scoping it to an Inbox category would be a lie). Assembled before the threads because
    # the selection decides which thread read runs.
    show_tabs = active == DEFAULT_LABEL and q is None
    tabs = await _mailbox_tabs(provider, mailbox, active) if show_tabs else []
    active_tab, tab_query = _resolve_tab(provider, tabs, tab)

    if mailbox is not None and q is None and tab_query is None and not cursor:
        bundle = await (mailbox.reconcile(active) if reconcile else mailbox.landing(active))
        labels, threads, next_cursor = bundle.labels, bundle.threads, bundle.next_cursor
    else:
        labels = await provider.list_labels(count_ids=(DEFAULT_LABEL, active))
        page = await provider.list_threads(
            label=None if q else active, query=q or tab_query, cursor=cursor, limit=capped
        )
        threads, next_cursor = page.threads, page.next_cursor
    return {
        "title": "Mail",
        "labels": [lbl.model_dump() for lbl in labels],
        "active_label": active,
        "query": q or "",
        "tabs": [tab_payload(category) for category in tabs],
        "active_tab": active_tab,
        "threads": [thread.model_dump() for thread in threads],
        "next_cursor": next_cursor,
    }


async def _mailbox_tabs(
    provider: MailProvider, mailbox: CachedMailbox | None, label: str
) -> list[MailCategory]:
    """The Inbox's category tabs, through the cache when there is one (#765).

    Without a *mailbox* orchestrator (a stateless build, or a unit test) this reads the
    provider directly — correct, just uncached; the whole point of the cached path is that
    assembling the tabs is a provider fan-out that must not run per render.
    """
    if mailbox is not None:
        return await mailbox.categories(label)
    return await provider.list_categories(label=label)


async def build_mailbox_thread(provider: MailProvider, thread_id: str) -> dict[str, Any]:
    """The `mailbox` thread read (ADR-0087): a full conversation + the reply prefill.

    Every message is rendered through the shared reader shape (:func:`message_payload`); the
    ``reply`` prefill derives the recipient/subject/threading from the **last** message via
    the tested :meth:`MailProvider.compose_reply` (#461) so a page reply threads correctly.
    The prefill carries only the last message's id — the actual send re-derives threading
    server-side, so the web never handles raw RFC-2822 headers.
    """
    thread = await provider.get_thread(thread_id)
    reply: dict[str, Any] | None = None
    if thread.messages:
        last = thread.messages[-1]
        composed = await provider.compose_reply(last.id, "")
        reply = {
            "reply_to_message_id": last.id,
            "to": composed.to,
            "subject": composed.subject,
            "reply_to_original": composed.reply_to_original
            or (f"{last.sender} — {thread.subject}" if last.sender else thread.subject),
        }
    return {
        "thread": {
            "id": thread.id,
            "subject": thread.subject,
            "messages": [message_payload(m) for m in thread.messages],
            "reply": reply,
        }
    }
