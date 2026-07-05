"""Shared upload guardrails (#175): one allowlist matcher + one set of default caps.

Both upload doors — chat attachments (``POST /platform/v1/agent/attachments``) and the
Files-page upload (``POST /platform/v1/files/upload``, #479) — enforce the same
operator-configured byte cap and content-type allowlist (``ATTACHMENT_MAX_BYTES`` /
``ATTACHMENT_ALLOWED_TYPES``): a file the operator may attach to a chat is exactly the
file they may put in the file space. nginx's ``client_max_body_size`` fronts both routes
under the one ``/platform/`` proxy block (services/web/nginx.conf.template).
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_ALLOWED_UPLOAD_TYPES: tuple[str, ...] = (
    "text/*",
    "image/*",
    "application/pdf",
    "application/json",
)


def content_type_allowed(content_type: str, allowed: Sequence[str]) -> bool:
    """Whether *content_type* matches the allowlist (supports ``type/*`` and ``*/*``)."""
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    for rule in allowed:
        if rule in ("*/*", ct):
            return True
        if rule.endswith("/*") and ct.startswith(rule[:-1]):
            return True
    return False
