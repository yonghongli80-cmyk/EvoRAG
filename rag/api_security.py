"""Shared API principal resolution and authorization helpers."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Mapping

from fastapi import Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from config import settings
from rag.auth import decode_token
from rag.permissions import check_doc_permission, check_kb_permission
from rag.user_db import UserDB

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
logger = logging.getLogger(__name__)

_SOURCE_SECRET_KEYS = frozenset({
    "authorization", "password", "token", "api_key", "apikey", "secret",
    "access_token", "refresh_token", "connection_string", "dsn",
})


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


def require_kb_view(db: UserDB, kb_id: str, user: dict) -> dict:
    """Require an explicit KB view grant; legacy metadata is admin-only."""
    meta = db.get_kb_metadata(kb_id)
    if meta is None:
        if user.get("is_admin"):
            return {"kb_id": kb_id, "owner_id": 0, "scope": "orphaned"}
        raise HTTPException(status_code=409, detail="知识库元数据缺失")
    check_kb_permission(db, kb_id, user, "view")
    return meta


def require_kb_edit(db: UserDB, kb_id: str, user: dict) -> dict:
    """Require an explicit KB edit grant and reject orphaned collections."""
    meta = db.get_kb_metadata(kb_id)
    if meta is None:
        raise HTTPException(status_code=409, detail="知识库元数据缺失")
    check_kb_permission(db, kb_id, user, "edit")
    return meta


def require_doc_view(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict:
    doc = db.get_document_permission(doc_name, kb_id)
    if doc is None:
        if user.get("is_admin"):
            return {"id": 0, "doc_name": doc_name, "kb_id": kb_id, "historical": True}
        raise HTTPException(status_code=403, detail="文档尚未建立权限记录")
    check_doc_permission(db, doc_name, kb_id, user, "view")
    return doc


def require_doc_version_edit(db: UserDB, document_id: int, user: dict) -> dict:
    """Resolve a document by id and require edit access on its logical document."""
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=409, detail="文档权限记录缺失")
    require_doc_edit(db, document["doc_name"], document["kb_id"], user)
    return document


def require_doc_edit(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict:
    doc = db.get_document_permission(doc_name, kb_id)
    if doc is None:
        raise HTTPException(status_code=409, detail="文档权限记录缺失")
    if doc.get("protected"):
        raise HTTPException(status_code=403, detail="受保护文档不可修改")
    check_doc_permission(db, doc_name, kb_id, user, "edit")
    return doc


async def require_data_sources_admin(
    request: Request,
    authorization: str = Header(default=""),
    api_key: str | None = Security(api_key_header),
) -> dict:
    """Expose data-source management only when explicitly enabled for admins."""
    if not settings.data_sources_enabled:
        raise HTTPException(status_code=404, detail="资源不存在")

    user = await require_user(request, authorization, api_key)
    return require_admin(user)


def redact_source_config(value: object) -> object:
    """Return a recursively redacted, safe-to-serialize source configuration."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SOURCE_SECRET_KEYS
            else redact_source_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_source_config(item) for item in value]
    return value


def log_security_mode() -> None:
    """Make the intentionally insecure local-development mode impossible to miss."""
    if not settings.auth_enabled:
        logger.critical(
            "Authentication is disabled because RAG_AUTH_ENABLED=false; "
            "do not use this setting in a shared or production environment."
        )
