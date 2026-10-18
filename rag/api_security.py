"""Shared API principal resolution and authorization helpers."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Mapping

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from config import settings
from rag.auth import decode_token
from rag.context import get_user_db as get_user_db
from rag.org_auth.audit import OrgAuditService
from rag.org_auth.models import OrgRole
from rag.org_auth.repository import OrgRepository, is_org_policy
from rag.org_auth.service import OrgAuthorizationService
from rag.permissions import check_doc_permission, check_kb_permission
from rag.repositories.analysis_repo import (
    _SENSITIVE_CONFIG_HINTS,
    _mask_url_credentials,
)
from rag.user_db import UserDB

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
logger = logging.getLogger(__name__)

_SOURCE_SECRET_KEYS = frozenset({
    "authorization", "password", "token", "api_key", "apikey", "secret",
    "access_token", "refresh_token", "connection_string", "dsn",
})

# Redaction policies for data-source configuration. A secret-named field is
# replaced as a whole: the user name, host, and database name inside a DSN are
# part of the credential surface, so ``postgresql://user:***@db/name`` hands an
# attacker half of the secret. Only credentials embedded inside a *non*-secret
# URL field keep the partial ``***`` presentation, because the host and path of
# such a URL are legitimately useful.
REDACTED = "[REDACTED]"


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
        user = _user_for_api_key(db, api_key)
        if user is None:
            return None
        person = OrgRepository(db).get_person_for_user(int(user["id"]))
        if person is not None and not person["active"]:
            OrgAuditService(db).record(
                int(user["id"]), "api_key_rejected", "person", person["person_code"],
                "denied", "account_inactive",
            )
            return None
        return user

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
    repo = OrgRepository(db)
    if not repo.token_is_current(user_id, payload.get("security_version")):
        person = repo.get_person_for_user(user_id)
        if person is not None:
            OrgAuditService(db).record(
                user_id, "token_rejected", "person", person["person_code"],
                "denied", "security_version_mismatch",
            )
        return None
    return user


def _get_current_user(token: str, db: UserDB | None = None) -> dict | None:
    """Resolve user from JWT token string."""
    if db is None:
        from rag.context import user_db
        db = user_db
    return resolve_principal(db, f"Bearer {token}", None)


async def _require_auth(authorization: str, db: UserDB | None = None) -> dict:
    """Require Bearer auth header and return user dict, or raise 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user_dict = await asyncio.to_thread(_get_current_user, token, db)
    if not user_dict:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return user_dict


async def require_user(
    request: Request,
    authorization: str = Header(default=""),
    api_key: str | None = Security(api_key_header),
) -> dict:
    if authorization or api_key:
        user = await asyncio.to_thread(resolve_principal, get_user_db(request), authorization, api_key)
        if not user:
            raise HTTPException(status_code=401, detail="未提供有效认证信息")
        return user

    if not settings.auth_enabled:
        user = await asyncio.to_thread(get_user_db(request).ensure_development_user)
        return {**user, "auth_mode": "disabled"}

    raise HTTPException(status_code=401, detail="未提供有效认证信息")


async def optional_user(
    request: Request,
    authorization: str = Header(default=""),
    api_key: str | None = Security(api_key_header),
) -> dict | None:
    if authorization or api_key:
        return await asyncio.to_thread(resolve_principal, get_user_db(request), authorization, api_key)

    if not settings.auth_enabled:
        user = await asyncio.to_thread(get_user_db(request).ensure_development_user)
        return {**user, "auth_mode": "disabled"}

    return None


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_kb_view(db: UserDB, kb_id: str, user: dict) -> dict:
    """Require an explicit KB view grant; legacy metadata is admin-only."""
    meta = db.get_kb_metadata(kb_id)
    policy = OrgRepository(db).get_kb_policy(kb_id)
    if policy is not None:
        decision = OrgAuthorizationService(db).authorize(int(user["id"]), kb_id, OrgRole.READ)
        if not decision.allowed:
            OrgAuditService(db).record(
                int(user["id"]), "kb_access_denied", "knowledge_base", kb_id,
                "denied", decision.reason,
            )
            raise HTTPException(status_code=404, detail="资源不存在")
        return {**(meta or {"kb_id": kb_id}), **policy, "effective_role": decision.role.value}
    if meta is None:
        if user.get("is_admin"):
            return {"kb_id": kb_id, "owner_id": 0, "scope": "orphaned"}
        raise HTTPException(status_code=409, detail="知识库元数据缺失")
    check_kb_permission(db, kb_id, user, "view")
    return meta


def require_kb_edit(db: UserDB, kb_id: str, user: dict) -> dict:
    """Require an explicit KB edit grant and reject orphaned collections."""
    meta = db.get_kb_metadata(kb_id)
    policy = OrgRepository(db).get_kb_policy(kb_id)
    if policy is not None:
        decision = OrgAuthorizationService(db).authorize(int(user["id"]), kb_id, OrgRole.MANAGE)
        if not decision.allowed:
            OrgAuditService(db).record(
                int(user["id"]), "kb_access_denied", "knowledge_base", kb_id,
                "denied", decision.reason,
            )
            raise HTTPException(status_code=404, detail="资源不存在")
        return {**(meta or {"kb_id": kb_id}), **policy, "effective_role": decision.role.value}
    if meta is None:
        raise HTTPException(status_code=409, detail="知识库元数据缺失")
    check_kb_permission(db, kb_id, user, "edit")
    return meta


def require_kb_administer(db: UserDB, kb_id: str, user: dict) -> dict:
    """Require company-administrator authority for an organization KB."""
    meta = db.get_kb_metadata(kb_id)
    policy = OrgRepository(db).get_kb_policy(kb_id)
    if not is_org_policy(policy):
        return require_kb_edit(db, kb_id, user)
    decision = OrgAuthorizationService(db).authorize(
        int(user["id"]), kb_id, OrgRole.ADMINISTER
    )
    if not decision.allowed:
        OrgAuditService(db).record(
            int(user["id"]), "kb_access_denied", "knowledge_base", kb_id,
            "denied", decision.reason,
        )
        raise HTTPException(status_code=404, detail="资源不存在")
    return {
        **(meta or {"kb_id": kb_id}),
        **policy,
        "effective_role": decision.role.value,
    }


def require_doc_view(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict:
    doc = db.get_document_permission(doc_name, kb_id)
    if is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        require_kb_view(db, kb_id, user)
        if doc is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        return doc
    if doc is None:
        if user.get("is_admin"):
            return {"id": 0, "doc_name": doc_name, "kb_id": kb_id, "historical": True}
        raise HTTPException(status_code=403, detail="文档尚未建立权限记录")
    check_doc_permission(db, doc_name, kb_id, user, "view")
    return doc


def require_personal_document_acl(db: UserDB, kb_id: str) -> None:
    """Reject legacy per-document sharing controls for organization KBs."""
    if is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        raise HTTPException(status_code=404, detail="资源不存在")


def require_doc_version_edit(db: UserDB, document_id: int, user: dict) -> dict:
    """Resolve a document by id and require edit access on its logical document."""
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=409, detail="文档权限记录缺失")
    require_doc_edit(db, document["doc_name"], document["kb_id"], user)
    return document


def require_doc_edit(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict:
    doc = db.get_document_permission(doc_name, kb_id)
    if is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        require_kb_edit(db, kb_id, user)
        if doc is None:
            raise HTTPException(status_code=404, detail="资源不存在")
        if doc.get("protected"):
            raise HTTPException(status_code=403, detail="受保护文档不可修改")
        return doc
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


def is_secret_config_key(key: object) -> bool:
    """Return *True* when *key* names a credential-bearing config field.

    Both key vocabularies are honoured: the exact names in
    :data:`_SOURCE_SECRET_KEYS` (which is what pins ``connection_string`` and
    ``dsn``) and the compound-name substrings in ``_SENSITIVE_CONFIG_HINTS``
    (which catch fields such as ``db_password`` or ``auth_token``).
    """
    low = str(key).lower()
    return low in _SOURCE_SECRET_KEYS or any(hint in low for hint in _SENSITIVE_CONFIG_HINTS)


def mask_source_config(value: object) -> tuple[object, bool]:
    """Recursively mask a data-source configuration.

    Returns ``(masked_value, has_secret)``. Secret-named keys are replaced by
    :data:`REDACTED` as a whole field, including when they hold a DSN; URLs that
    appear under any *other* key still lose their userinfo and sensitive query
    parameters, so a credential smuggled into a plain ``url`` field is masked too.
    """
    if isinstance(value, Mapping):
        masked: dict = {}
        has_secret = False
        for key, item in value.items():
            if is_secret_config_key(key) and item not in (None, ""):
                masked[str(key)] = REDACTED
                has_secret = True
                continue
            sub_masked, sub_has_secret = mask_source_config(item)
            masked[str(key)] = sub_masked
            has_secret = has_secret or sub_has_secret
        return masked, has_secret
    if isinstance(value, list):
        items = []
        has_secret = False
        for item in value:
            sub_masked, sub_has_secret = mask_source_config(item)
            items.append(sub_masked)
            has_secret = has_secret or sub_has_secret
        return items, has_secret
    if isinstance(value, str) and "://" in value:
        return _mask_url_credentials(value)
    return value, False


def redact_source_config(value: object) -> object:
    """Return a recursively redacted, safe-to-serialize source configuration."""
    return mask_source_config(value)[0]


def log_security_mode() -> None:
    """Make the intentionally insecure local-development mode impossible to miss."""
    if not settings.auth_enabled:
        logger.critical(
            "Authentication is disabled because RAG_AUTH_ENABLED=false; "
            "do not use this setting in a shared or production environment."
        )
