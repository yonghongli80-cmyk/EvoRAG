"""Shared API principal resolution and authorization helpers."""

from __future__ import annotations

import asyncio
import hmac
import json

from fastapi import Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from config import settings
from rag.auth import decode_token
from rag.user_db import UserDB

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_user_db(request: Request) -> UserDB:
    return request.app.state.user_db


def _user_for_api_key(db: UserDB, api_key: str) -> dict | None:
    try:
        configured = json.loads(settings.auth_keys)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="鉴权配置格式错误") from exc

    for principal, expected in configured.items():
        if not hmac.compare_digest(api_key, str(expected)):
            continue
        if str(principal).isdigit():
            return db.get_user_by_id(int(principal))
        return db.get_user_by_username(str(principal))
    return None


def resolve_principal(db: UserDB, authorization: str, api_key: str | None) -> dict | None:
    if api_key:
        return _user_for_api_key(db, api_key)

    scheme, separator, token = authorization.partition(" ")
    if scheme != "Bearer" or separator != " " or not token or " " in token:
        return None

    payload = decode_token(token)
    if not payload or "user_id" not in payload:
        return None
    try:
        user_id = int(payload["user_id"])
    except (TypeError, ValueError):
        return None

    user = db.get_user_by_id(user_id)
    if not user or decode_token(token, user.get("password_changed_at")) is None:
        return None
    return user


async def require_user(
    request: Request,
    authorization: str = Header(default=""),
    api_key: str | None = Security(api_key_header),
) -> dict:
    if not settings.auth_enabled:
        return {
            "id": 0,
            "username": "anonymous-development",
            "is_admin": True,
            "auth_mode": "disabled",
        }

    user = await asyncio.to_thread(resolve_principal, get_user_db(request), authorization, api_key)
    if not user:
        raise HTTPException(status_code=401, detail="未提供有效认证信息")
    return user


def require_admin(user: dict) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
