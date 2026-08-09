"""API Key 鉴权模块"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, Header, Security
from fastapi.security import APIKeyHeader
from jose import JWTError, jwt

from config import settings

# ---------------------------------------------------------------------------
# JWT Configuration
# ---------------------------------------------------------------------------
_JWT_ALGORITHM: str = "HS256"
_JWT_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "jwt_secret.txt"


def _load_or_create_secret(secret_file: Path = _JWT_SECRET_FILE) -> str:
    """Load JWT secret from env > file > generate new."""
    env_secret = os.environ.get("RAG_JWT_SECRET")
    if env_secret:
        return env_secret
    if secret_file.exists():
        return secret_file.read_text().strip()
    secret = secrets.token_urlsafe(32)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(secret)
    return secret


_JWT_SECRET: str = _load_or_create_secret()


# ---------------------------------------------------------------------------
# Password hashing (pbkdf2_hmac with random salt)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hash *password* with a random 16-byte salt using PBKDF2-HMAC-SHA256.

    Returns ``salt_hex$hash_hex``.
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations=260_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Return True if *password* matches the ``salt$hash`` in *stored*."""
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations=260_000)
    return hmac.compare_digest(dk.hex(), hash_hex)


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
    except JWTError:
        return None
    # NULL 跳过检查（迁移后不踢用户）
    if password_changed_at and payload.get("iat", 0) < password_changed_at:
        return None
    return payload


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: str = Security(api_key_header), authorization: str = Header(default="")) -> str:
    """校验 API Key 或 JWT Token，返回 user_id。auth_enabled=False 时跳过校验。"""
    if not settings.auth_enabled:
        return "anonymous"
    # Try X-API-Key first
    if api_key:
        try:
            keys = json.loads(settings.auth_keys)
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="鉴权配置格式错误")
        for user_id, key in keys.items():
            if hmac.compare_digest(api_key, key):
                return user_id
    # Fallback: try JWT Bearer token
    if authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
        payload = decode_token(token)
        if payload and "user_id" in payload:
            # Import lazily to avoid the api.py -> auth.py import cycle.
            from rag.api import user_db

            user = user_db.get_user_by_id(payload["user_id"])
            if not user:
                payload = None
            else:
                payload = decode_token(token, user.get("password_changed_at"))
        if payload and "user_id" in payload:
            return str(payload["user_id"])
    raise HTTPException(status_code=403, detail="认证失败，请提供有效的 API Key 或 JWT Token")
