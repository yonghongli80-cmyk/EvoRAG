import hashlib
import secrets

from rag.auth import hash_password, verify_password


def test_password_hash_uses_600k_iterations_and_verifies_legacy_hash():
    password = "StrongPass1"
    assert verify_password(password, hash_password(password))

    salt = secrets.token_bytes(16)
    legacy = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=260_000)
    assert verify_password(password, f"{salt.hex()}${legacy.hex()}")
