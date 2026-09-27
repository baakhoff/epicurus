"""The sign-in failure vocabulary — one closed set, shared by the core and the web shell (#969).

Every way a sign-in can fail ends the same way for the browser: a ``302`` to
``/?auth_error=<code>``, because the callback is a top-level navigation and must never land on a
JSON page. The web shell renders each code as a sentence, so the set is a contract: adding a
code here without teaching the shell to say it is how a user ends up reading an identifier.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, get_args

__all__ = ["AUTH_ERROR_CODES", "AuthErrorCode", "AuthFlowError"]

AuthErrorCode = Literal[
    # Discovery / JWKS / the token endpoint could not be reached (a network failure or a 5xx).
    "provider_unreachable",
    # The provider answered the authorization request with an error other than access_denied,
    # or with a callback that carries neither a code nor an error.
    "provider_error",
    # The provider says the user declined (or was refused) — `error=access_denied`.
    "access_denied",
    # The login transaction is missing, unknown, expired, or already used.
    "state_mismatch",
    # The token endpoint refused the code, or answered without an ID token.
    "token_exchange_failed",
    # The ID token (or the callback's `iss`) failed validation.
    "invalid_token",
    # Authenticated, but no admission rule admits this identity.
    "not_allowed",
    # A groups allowlist is set, no `groups` claim arrived at all, and the email did not admit.
    "groups_claim_missing",
    # The email is on the allowlist, but the provider says it is not verified.
    "email_unverified",
    # The provider's metadata does not fit this client, or the core could not run the flow.
    "misconfigured",
]

AUTH_ERROR_CODES: Final[tuple[str, ...]] = get_args(AuthErrorCode)


class AuthFlowError(Exception):
    """A sign-in step failed. ``code`` is what the browser is redirected with.

    ``message`` is for the log line — it names what went wrong in operator terms and never
    carries a token, a code, a nonce, a verifier or a secret.
    """

    def __init__(
        self, code: AuthErrorCode, message: str, *, claims: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code: AuthErrorCode = code
        self.message = message
        #: The verified claims, when the refusal came after the provider said who this is —
        #: so the refusal's log line can name the subject and email the operator must look up.
        self.claims = claims
