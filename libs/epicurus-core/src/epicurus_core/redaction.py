"""Secret-key redaction — the one rule that keeps credentials out of surfaced data.

ADR-0031 gave the live log console a guarantee: a structured-log key whose *name* looks
like a credential never leaves the process. That rule lived as a private frozenset inside
``core-app``'s ``log_stream`` module, which was fine while the log console was the only
surface that showed operator-facing structured data. The event spine adds a second one —
the raw events feed — and a second copy of a security rule is how the two drift apart
(the same copy-paste that ``epicurus_core.db`` was created to end, ADR-0067).

So the predicate lives here, in the lowest shared layer, and every surface imports it:
the log console redacts a log entry's ``context``, and the event spine both *rejects* a
secret-shaped payload key at emit (:mod:`epicurus_core.module_events`) and redacts
defensively again at the feed. One list, audited in one place.

Matching is deliberately blunt — a case-insensitive **substring** test on the key name,
never the value. ``api_key``, ``X-Auth-Token``, and ``refresh_token`` all match on
``key`` / ``auth`` / ``token``. It over-matches by design (a key named ``monkey`` is
redacted); a false positive costs one hidden debugging field, a false negative leaks a
credential to a browser tab, and those are not symmetric.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["REDACTED_KEYS", "is_secret_key", "redact_mapping", "secret_keys_in"]

REDACTED_KEYS: frozenset[str] = frozenset(
    {"token", "key", "secret", "password", "credential", "auth", "api_key"}
)
"""Substrings that mark a key name as credential-shaped. Case-insensitive."""


def is_secret_key(key: str) -> bool:
    """Whether *key*'s name looks like it holds a credential."""
    lower = key.lower()
    return any(marker in lower for marker in REDACTED_KEYS)


def secret_keys_in(data: Mapping[str, Any]) -> list[str]:
    """The credential-shaped key names in *data*, sorted — for a "reject this" error.

    Only the top level: the spine caps payloads at a size that makes deep nesting
    impractical, and a flat pointer payload is the contract (see
    :mod:`epicurus_core.module_events`).
    """
    return sorted(key for key in data if is_secret_key(key))


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """*data* without its credential-shaped keys — the defensive pass at a surface."""
    return {key: value for key, value in data.items() if not is_secret_key(key)}
