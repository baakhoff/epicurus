"""VAPID (RFC 8292) keypair generation and reconstruction — the pure crypto, no I/O.

A tenant's keypair is generated lazily on first use and stored in OpenBao; see
:meth:`epicurus_core_app.push.service.PushService._vapid_keypair`. Kept dependency-free
of any store so it's trivially unit-testable.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01

__all__ = ["generate_vapid_keypair", "load_vapid_signer"]


def _b64url(raw: bytes) -> str:
    """Base64url, unpadded — the encoding VAPID and the browser Push API both use."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_vapid_keypair() -> tuple[str, str]:
    """Generate a fresh EC (P-256) VAPID keypair.

    Returns ``(private_key_pem, public_key_b64url)``. The PEM is what
    :func:`load_vapid_signer` reconstructs a signer from; the base64url string is exactly
    the ``applicationServerKey`` bytes the browser's ``PushManager.subscribe`` expects
    (the raw uncompressed EC point, RFC 8292 / RFC 8291 formats).
    """
    vapid = Vapid01()
    vapid.generate_keys()
    private_pem = vapid.private_pem().decode("ascii")
    public_bytes = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return private_pem, _b64url(public_bytes)


def load_vapid_signer(private_key_pem: str) -> Vapid01:
    """Reconstruct a signer from a stored PEM private key, for one ``webpush()`` call."""
    return Vapid01.from_pem(private_key_pem.encode("ascii"))
