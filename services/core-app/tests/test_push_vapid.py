"""Unit tests for VAPID keypair generation/reconstruction (pure crypto, no I/O)."""

from __future__ import annotations

import base64

from epicurus_core_app.push.vapid import generate_vapid_keypair, load_vapid_signer


def test_generate_returns_a_pem_and_a_base64url_public_key() -> None:
    private_pem, public_key = generate_vapid_keypair()
    assert private_pem.startswith("-----BEGIN")
    # No padding, and every char is in the base64url alphabet (no '+' or '/').
    assert "=" not in public_key
    assert "+" not in public_key and "/" not in public_key


def test_public_key_decodes_to_an_uncompressed_p256_point() -> None:
    _, public_key = generate_vapid_keypair()
    padded = public_key + "=" * (-len(public_key) % 4)
    raw = base64.urlsafe_b64decode(padded)
    assert len(raw) == 65  # 0x04 prefix + 32-byte X + 32-byte Y
    assert raw[0] == 0x04


def test_each_call_generates_a_distinct_keypair() -> None:
    first_private, first_public = generate_vapid_keypair()
    second_private, second_public = generate_vapid_keypair()
    assert first_private != second_private
    assert first_public != second_public


def test_load_vapid_signer_reconstructs_from_the_stored_pem() -> None:
    """A signer rebuilt from the stored PEM signs claims exactly like the original."""
    private_pem, public_key = generate_vapid_keypair()
    signer = load_vapid_signer(private_pem)
    headers = signer.sign({"sub": "mailto:test@example.com", "aud": "https://push.example.com"})
    assert headers["Authorization"]
    # Vapid01's Crypto-Key header carries the same public key generate_vapid_keypair() returned
    # (RFC 8291's applicationServerKey), proving the reconstructed signer matches the original.
    assert public_key in headers["Crypto-Key"]
