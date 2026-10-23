"""API Key 鉴权模块"""

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

import jwt
from fastapi import Header, HTTPException, Security
from fastapi.security import APIKeyHeader

from config import settings

# ---------------------------------------------------------------------------
# JWT Configuration
# ---------------------------------------------------------------------------
_JWT_ALGORITHM: str = "HS256"
_JWT_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "jwt_secret.txt"


def _dotenv_secret() -> str:
    """Read RAG_JWT_SECRET from the repository .env without exporting it."""
    env_file = _JWT_SECRET_FILE.parent.parent / ".env"
    try:
        from dotenv import dotenv_values

        return (dotenv_values(env_file).get("RAG_JWT_SECRET") or "").strip()
    except Exception:
        return ""


def _load_or_create_secret(secret_file: Path = _JWT_SECRET_FILE) -> str:
    """Load JWT secret from process env > .env > file > generate new."""
    env_secret = (os.environ.get("RAG_JWT_SECRET") or _dotenv_secret()).strip()
    if env_secret:
        return env_secret
    if secret_file.exists():
        return secret_file.read_text().strip()
    secret = secrets.token_urlsafe(32)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(secret)
    os.chmod(secret_file, 0o600)
    return secret


_JWT_SECRET: str = _load_or_create_secret()
_PBKDF2_ITERATIONS = 600_000
_LEGACY_PBKDF2_ITERATIONS = 260_000


# ---------------------------------------------------------------------------
# Password hashing (pbkdf2_hmac with random salt)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hash *password* with a random 16-byte salt using PBKDF2-HMAC-SHA256.

    Returns ``salt_hex$hash_hex``.
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations=_PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Return True if *password* matches the ``salt$hash`` in *stored*."""
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    for iterations in (_PBKDF2_ITERATIONS, _LEGACY_PBKDF2_ITERATIONS):
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations=iterations)
        if hmac.compare_digest(dk.hex(), hash_hex):
            return True
    return False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def create_token(payload: dict, expires_seconds: int = 86400) -> str:
    """Create a JWT token with *payload* and a default 24-hour expiry."""
    to_encode = payload.copy()
    to_encode["exp"] = datetime.now(UTC).timestamp() + expires_seconds
    to_encode.setdefault("iat", datetime.now(UTC).timestamp())
    return jwt.encode(to_encode, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def decode_token(token: str, password_changed_at: float = None) -> dict | None:
    """Decode a JWT token. Returns the payload dict, or *None* if invalid/expired.

    password_changed_at: 如果非 None，检查 token.iat < password_changed_at 则返回 None。
    NULL 跳过检查（迁移后不踢用户）。
    """
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    # NULL 跳过检查（迁移后不踢用户）
    if password_changed_at and payload.get("iat", 0) < password_changed_at:
        return None
    return payload


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: str = Security(api_key_header), authorization: str = Header(default="")) -> str:
    """校验 API Key 或 JWT Token，返回 user_id。auth_enabled=False 时跳过校验。

    Both credential kinds resolve through :func:`rag.api_security.resolve_principal`
    (B-04).  The old X-API-Key branch returned the configured principal name
    verbatim, so a key for a deleted or deactivated account still authenticated.
    The API key itself carries no token version; account activation (and, for
    Bearer tokens, ``security_version``) is therefore the shared revocation
    boundary.
    """
    if not settings.auth_enabled:
        return "anonymous"
    # Import lazily to avoid the api.py -> auth.py import cycle.
    from rag.api_security import resolve_principal
    from rag.context import get_user_db

    db = get_user_db()
    if api_key:
        user = resolve_principal(db, "", api_key)
        if not user:
            raise HTTPException(status_code=403, detail="认证失败，请提供有效的 API Key 或 JWT Token")
        return str(user["id"])
    # Fallback: resolve JWT through the same principal boundary as modern APIs.
    if authorization.startswith("Bearer "):
        user = resolve_principal(db, authorization, None)
        if user:
            return str(user["id"])
    raise HTTPException(status_code=403, detail="认证失败，请提供有效的 API Key 或 JWT Token")
