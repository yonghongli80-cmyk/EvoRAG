"""Tests for JWT authentication helpers in rag.auth."""

import time
from datetime import UTC

import jwt

from rag.auth import (
    create_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_and_verify():
    """Hash a password, then verify correct and wrong passwords."""
    pw = "s3cret-p@ss"
    stored = hash_password(pw)

    # Stored value should contain salt$hash
    assert "$" in stored

    # Correct password verifies
    assert verify_password(pw, stored) is True

    # Wrong password fails
    assert verify_password("wrong-password", stored) is False


def test_create_and_decode_token():
    """Create a token, decode it, and check the payload is preserved."""
    payload = {"sub": "user123", "role": "admin"}
    token = create_token(payload, expires_seconds=3600)

    decoded = decode_token(token)
    assert decoded is not None
    assert decoded["sub"] == "user123"
    assert decoded["role"] == "admin"
    assert "exp" in decoded


def test_decode_expired_token():
    """A token whose expiry is in the past should be rejected."""
    from datetime import datetime

    from rag.auth import _JWT_ALGORITHM, _JWT_SECRET

    # Build a token that already expired 1 second ago
    payload = {"sub": "user123", "exp": datetime.now(UTC).timestamp() - 1}
    token = jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)

    time.sleep(0.1)
    assert decode_token(token) is None


def test_decode_invalid_token():
    """Decoding garbage should return None."""
    assert decode_token("not.a.valid.token") is None
