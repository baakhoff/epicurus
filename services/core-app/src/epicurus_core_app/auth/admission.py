"""Who may sign in: the admission rule, applied to the verified claims (#969).

The provider answers *who* this is; this module answers whether they may come in. Three rules,
any of which admits:

* ``OIDC_ALLOW_ALL_USERS=true`` — everyone the provider authenticates for this client. For a
  private provider (a Pocket ID that only knows the household) that is the whole policy.
* the lowercased ``email`` is on ``OIDC_ALLOWED_EMAILS`` — **and** the provider has not said
  the address is unverified. A provider that lets a user type any email address would
  otherwise let them type the operator's.
* the ``groups`` claim shares a group with ``OIDC_ALLOWED_GROUPS``. A list of strings is the
  norm; a single string is tolerated (some providers flatten a one-element list).

A refusal names the most useful reason, because the operator reads it to fix the rule: an
allowlisted but unverified address says so; a groups rule with no ``groups`` claim at all says
*that* (almost always a missing ``groups`` scope, not a user in the wrong group); anything
else is plainly ``not_allowed``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from epicurus_core_app.auth.config import AuthConfig
from epicurus_core_app.auth.errors import AuthErrorCode

__all__ = ["admit", "claim_email", "claim_groups"]


def claim_email(claims: Mapping[str, Any]) -> str | None:
    """The ``email`` claim, trimmed and lowercased, or ``None`` if there isn't a usable one."""
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    return email.strip().lower()


def claim_groups(claims: Mapping[str, Any]) -> list[str] | None:
    """The ``groups`` claim as a list of strings; ``None`` when the claim is absent entirely.

    Absent and empty are different answers: ``[]`` is "a member of nothing", ``None`` is "the
    provider did not say", which is what ``groups_claim_missing`` reports.
    """
    if "groups" not in claims or claims["groups"] is None:
        return None
    raw = claims["groups"]
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [group for group in raw if isinstance(group, str) and group]
    return []


def _explicitly_unverified(claims: Mapping[str, Any]) -> bool:
    """``email_verified`` is ``false`` — as a boolean, or as the string some providers send."""
    verified = claims.get("email_verified")
    if isinstance(verified, bool):
        return not verified
    return isinstance(verified, str) and verified.strip().lower() == "false"


def admit(claims: Mapping[str, Any], config: AuthConfig) -> AuthErrorCode | None:
    """``None`` if *claims* are admitted, else the refusal code to redirect with."""
    if config.allow_all_users:
        return None

    email_listed = False
    email = claim_email(claims)
    if config.allowed_emails and email is not None and email in config.allowed_emails:
        email_listed = True
        if not _explicitly_unverified(claims):
            return None

    groups = claim_groups(claims)
    if config.allowed_groups and groups and config.allowed_groups.intersection(groups):
        return None

    if email_listed:
        return "email_unverified"
    if config.allowed_groups and groups is None:
        return "groups_claim_missing"
    return "not_allowed"
