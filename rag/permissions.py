"""文档权限校验工具函数。

三档权限模型：
- private：仅 owner 可见
- shared：owner + 指定用户可见（view 或 edit 权限）
- public：所有人可见
- admin 绕过所有限制
- protected 文件始终 public，不可修改 scope
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from rag.user_db import UserDB


def check_doc_permission(
    db: UserDB,
    doc_name: str,
    kb_id: str,
    user: dict,
    action: str = "view",
) -> dict | None:
    """校验用户对指定文档的操作权限。

    规则：
    - 无权限记录（旧文档）→ 放行
    - admin → 放行
    - 查看：owner / scope='public' / scope='shared' 且在 shares 表中 → 放行
    - 编辑/删除：仅 owner / scope='shared' 且 permission='edit' → 放行

    Returns:
        document_permission 记录 dict（有记录时）
        None（无权限记录 — 旧文档，放行）

    Raises:
        HTTPException 403: 无权操作
    """
    doc = db.get_document_permission(doc_name, kb_id)

    # Historical documents without an ACL are visible only to administrators.
    if not doc:
        if user.get("is_admin"):
            return None
        raise HTTPException(status_code=403, detail="文档尚未建立权限记录")

    # 管理员绕过
    if user.get("is_admin"):
        return doc

    scope = doc.get("scope", "private")

    if action == "view":
        # owner / public → 可查看
        if doc["owner_id"] == user["id"] or scope == "public":
            return doc
        if scope == "shared" and db.is_document_shared(doc["id"], user["id"]):
            return doc
        raise HTTPException(status_code=403, detail="无权查看该文档")

    if action in ("edit", "delete"):
        # owner → 可操作
        if doc["owner_id"] == user["id"]:
            return doc
        # shared + permission='edit' → 可编辑
        if scope == "shared" and db.is_document_shared(doc["id"], user["id"], permission="edit"):
            return doc
        raise HTTPException(status_code=403, detail="仅文档上传者可操作")

    raise HTTPException(status_code=400, detail=f"未知操作: {action}")


def check_kb_permission(
    db: UserDB,
    kb_id: str,
    user: dict,
    action: str = "view",
) -> dict | None:
    """校验用户对知识库的操作权限。

    规则：
    - 无元数据（旧 KB）→ 放行
    - admin → 放行
    - 查看：owner / scope='public' / scope='shared' 且在 kb_shares 表中 → 放行
    - 编辑/删除：仅 owner / scope='shared' 且 permission='edit' → 放行

    Returns:
        kb_metadata dict（有记录时）
        None（无记录 — 旧 KB，放行）

    Raises:
        HTTPException 403: 无权操作
    """
    meta = db.get_kb_metadata(kb_id)
    if not meta:
        if user.get("is_admin"):
            return None
        raise HTTPException(status_code=403, detail="知识库尚未建立元数据")

    if user.get("is_admin"):
        return meta

    scope = meta.get("scope", "private")

    if action == "view":
        if meta["owner_id"] == user["id"] or scope == "public":
            return meta
        if scope == "shared" and db.is_kb_shared(kb_id, user["id"]):
            return meta
        raise HTTPException(status_code=403, detail="无权查看该知识库")

    if action in ("edit", "delete"):
        if meta["owner_id"] == user["id"]:
            return meta
        if scope == "shared" and db.is_kb_shared(kb_id, user["id"], permission="edit"):
            return meta
        raise HTTPException(status_code=403, detail="无权操作该知识库")

    raise HTTPException(status_code=400, detail=f"未知操作: {action}")


def get_accessible_doc_names(db: UserDB, kb_id: str, user: dict) -> list[str]:
    """返回用户在指定知识库中有权查看的文档名列表。

    规则：owner 或 is_public 的文档可见。admin 返回 None（不过滤）。
    """
    if user.get("is_admin"):
        return None  # None 表示不过滤

    return db.get_accessible_doc_names(
        kb_id=kb_id,
        user_id=user["id"],
    )


def filter_chunks_by_permission(
    db: UserDB,
    kb_id: str,
    chunks: list,
    user: dict,
) -> list:
    """过滤掉用户无权查看的文档的 chunks。

    规则：
    - 管理员：不过滤
    - 旧文档无权限记录：视为公开，保留
    - owner 或 is_public：保留
    - 其他：过滤掉
    """
    if user.get("is_admin"):
        return list(chunks)

    # 批量查询：一次性查出所有 doc_name 的权限记录（避免 N+1）
    doc_names = list(set(c.doc_name for c in chunks))
    perm_map = db.get_document_permissions_by_names(doc_names, kb_id)

    # 查出用户有权查看的文档名
    allowed_names = db.get_accessible_doc_names(
        kb_id=kb_id,
        user_id=user["id"],
    )
    allowed_set = set(allowed_names)

    result = []
    for c in chunks:
        perm = perm_map.get(c.doc_name)
        if perm is not None and c.doc_name in allowed_set:
            # 有权限记录且用户有权查看
            result.append(c)
        # else: 有权限记录但用户无权，过滤掉

    return result
