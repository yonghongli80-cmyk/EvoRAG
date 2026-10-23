"""Authentication and user management routes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from config import settings
from rag.api_security import get_user_db, require_user
from rag.auth import create_token
from rag.login_throttle import LoginThrottle
from rag.org_auth.audit import OrgAuditService
from rag.org_auth.repository import OrgRepository
from rag.user_db import _validate_password_strength

logger = logging.getLogger(__name__)

router = APIRouter()

_SESSION_COOKIE = "rag_session"
_login_throttle = LoginThrottle(
    settings.login_failure_limit,
    settings.login_failure_window_seconds,
)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=8, description="密码至少8位，需含大写字母、小写字母和数字")


class LoginRequest(BaseModel):
    # Bound credential fields before hashing/throttling.
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResponse(BaseModel):
    token: str
    username: str


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="旧密码")
    new_password: str = Field(..., description="新密码")
    confirm_password: str = Field(..., description="确认新密码")


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., description="新密码")


class UpdateRoleRequest(BaseModel):
    is_admin: bool | None = None


@router.post("/register", response_model=TokenResponse, summary="用户注册")
async def register(req: RegisterRequest, request: Request):
    if not settings.self_registration_enabled:
        raise HTTPException(status_code=404, detail="资源不存在")
    try:
        _validate_password_strength(req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    db = get_user_db(request)
    try:
        user_id = await asyncio.to_thread(db.create_user, req.username, req.password)
    except ValueError as e:
        if "已存在" in str(e):
            raise HTTPException(status_code=400, detail="用户名或密码错误") from e
        raise HTTPException(status_code=400, detail=str(e)) from e
    token = create_token({"user_id": user_id, "username": req.username})
    return TokenResponse(token=token, username=req.username)


@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(req: LoginRequest, request: Request, response: Response):
    client_host = request.client.host if request.client else "unknown"
    throttle_key = f"{client_host}:{req.username.strip().casefold()}"
    if _login_throttle.blocked(throttle_key):
        raise HTTPException(status_code=429, detail="登录尝试过多，请稍后重试")

    db = get_user_db(request)

    def _resolve_login_name(value: str) -> str:
        with db.locked_read() as conn:
            rows = conn.execute(
                "SELECT u.username FROM org_people p JOIN users u ON u.id=p.user_id "
                "WHERE p.active=1 AND p.display_name=? LIMIT 2",
                (value.strip(),),
            ).fetchall()
        return rows[0]["username"] if len(rows) == 1 else value.strip()

    login_name = await asyncio.to_thread(_resolve_login_name, req.username)
    user = await asyncio.to_thread(db.authenticate, login_name, req.password)
    if not user:
        _login_throttle.record_failure(throttle_key)
        await asyncio.to_thread(
            OrgAuditService(db).record,
            None,
            "login_failed",
            "authentication",
            "",
            "denied",
            "invalid_credentials",
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    profile = await asyncio.to_thread(OrgRepository(db).get_profile_for_user, user["id"])
    if profile is not None and not profile.active:
        await asyncio.to_thread(
            OrgAuditService(db).record,
            int(user["id"]),
            "login_failed",
            "person",
            profile.person_code,
            "denied",
            "account_inactive",
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    _login_throttle.clear(throttle_key)
    token = create_token({
        "user_id": user["id"],
        "username": user["username"],
        "security_version": profile.security_version if profile else None,
    })
    if profile is not None:
        await asyncio.to_thread(
            OrgAuditService(db).record,
            int(user["id"]),
            "login_success",
            "person",
            profile.person_code,
            "success",
            "authenticated",
        )
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    secure_cookie = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=secure_cookie,
        max_age=86400,
        path="/",
    )
    return TokenResponse(token=token, username=user["username"])


@router.post("/logout", status_code=204, summary="退出登录")
async def logout(response: Response):
    response.delete_cookie(_SESSION_COOKIE, path="/", samesite="strict")


@router.get("/me", summary="获取当前用户信息")
async def get_me(request: Request, user: dict = Depends(require_user)):
    db = get_user_db(request)
    profile = await asyncio.to_thread(OrgRepository(db).get_profile_for_user, user["id"])
    result = {"id": user["id"], "username": user["username"]}
    if profile is not None:
        result.update({"is_admin": bool(user.get("is_admin")), "profile": asdict(profile)})
    return result


@router.get("/users", summary="搜索用户", description="按用户名搜索用户，用于共享功能")
async def search_users(
    request: Request,
    q: str = Query(..., min_length=2, description="搜索词，至少 2 个字符"),
    user: dict = Depends(require_user),
):
    db = get_user_db(request)

    def _search():
        rows = db.search_users(q, limit=20)
        return [{"id": r["id"], "username": r["username"]} for r in rows]

    return await asyncio.to_thread(_search)


@router.put("/users/me/password", summary="修改密码")
async def change_password(
    request: Request,
    req: ChangePasswordRequest,
    user: dict = Depends(require_user),
):
    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="新密码与确认密码不匹配")

    db = get_user_db(request)
    try:
        await asyncio.to_thread(
            db.change_password, user["id"], req.old_password, req.new_password
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"message": "密码已修改，请重新登录"}


@router.put("/users/{uid}/reset-password", summary="管理员重置密码")
async def reset_password(
    request: Request,
    uid: int,
    req: ResetPasswordRequest,
    user: dict = Depends(require_user),
):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可重置密码")

    db = get_user_db(request)
    try:
        await asyncio.to_thread(db.reset_password, uid, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"message": "密码已重置"}


def _audit_role_change(
    db,
    actor: dict,
    target: dict,
    granted: bool,
    previous: bool,
    result: str,
    reason: str,
) -> None:
    """Record one administrator-flag change attempt (B-03).

    Successful changes *and* refused attempts are audited: knowing that somebody
    tried to demote the last administrator is the point of the audit trail.
    """
    OrgAuditService(db).record(
        int(actor["id"]),
        "user_role_changed",
        "user",
        str(target["id"]),
        result,
        reason,
        {
            "is_admin": bool(granted),
            "previous_is_admin": bool(previous),
            "username": target.get("username", ""),
        },
    )


def _count_other_admins(db, user_id: int) -> int:
    """Return how many administrators would remain if *user_id* lost the flag."""
    with db.locked_read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND id <> ?", (user_id,)
        ).fetchone()
    return int(row[0])


def _apply_role_change(db, actor: dict, target: dict, granted: bool) -> bool:
    """Validate and apply one administrator-flag change, audited either way.

    Guards (B-03):
    - an administrator cannot revoke their own flag (no accidental lock-out);
    - the last remaining administrator cannot be demoted (defense in depth for
      callers other than this endpoint).

    Returns ``True`` when the stored value changed.
    """
    previous = bool(target["is_admin"])
    if previous == granted:
        _audit_role_change(db, actor, target, granted, previous, "success", "unchanged")
        return False
    if not granted:
        if int(target["id"]) == int(actor["id"]):
            _audit_role_change(db, actor, target, granted, previous, "denied", "self_demotion")
            raise HTTPException(status_code=403, detail="不能撤销自己的管理员权限")
        if _count_other_admins(db, int(target["id"])) == 0:
            _audit_role_change(
                db, actor, target, granted, previous, "denied", "last_administrator"
            )
            raise HTTPException(status_code=409, detail="不能撤销最后一名管理员的权限")
    db.set_user_admin(int(target["id"]), granted)
    _audit_role_change(db, actor, target, granted, previous, "success", "granted" if granted else "revoked")
    return True


@router.put("/users/{uid}/role", summary="设置用户角色（管理员/权限等级）")
async def set_user_role(
    request: Request,
    uid: int,
    req: UpdateRoleRequest,
    user: dict = Depends(require_user),
):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    db = get_user_db(request)

    def _set():
        target = db.get_user_by_id(uid)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        if req.is_admin is not None:
            _apply_role_change(db, user, target, req.is_admin)
        return db.get_user_by_id(uid)

    return await asyncio.to_thread(_set)
