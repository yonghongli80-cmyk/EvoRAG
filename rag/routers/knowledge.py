"""Knowledge base management routes."""

from __future__ import annotations

import asyncio
import json as _json
import logging
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from config import settings
from rag.api_security import (
    get_user_db,
    is_org_policy,
    require_doc_edit,
    require_kb_administer,
    require_kb_edit,
    require_kb_view,
    require_user,
    resolve_principal,
)
from rag.context import (
    DATA_DIR,
    KB_FILES_ROOT,
    MAX_FILE_SIZE,
    SUPPORTED_EXTENSIONS,
)
from rag.context import (
    user_db as default_user_db,
)
from rag.document_sources.upload import UploadDocumentSource
from rag.document_versions import (
    DuplicateDocument,
    choose_document_name,
    ingest_document_version,
)
from rag.kb_file_store import resolve_stored_path
from rag.kb_metadata import generate_summary, generate_toc
from rag.knowledge_base import KnowledgeBaseManager
from rag.loader import load as load_document
from rag.org_auth.audit import OrgAuditService
from rag.org_auth.repository import OrgRepository
from rag.org_auth.service import OrgAuthorizationService
from rag.permissions import filter_chunks_by_permission
from rag.routers.documents import ShareRequest
from rag.spreadsheets.migration import ensure_structured_dataset

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Knowledge"])


class CreateKBRequest(BaseModel):
    name: str = Field(..., description="知识库名称")
    scope: str = Field(default="private", pattern="^(private|public)$", description="可见范围：private 或 public")
    org_unit_code: str | None = Field(default=None, description="所属组织编号")


class KBResponse(BaseModel):
    kb_id: str
    name: str
    doc_count: int
    scope: str = "public"
    is_owner: bool = False
    shared_users: list = Field(default_factory=list, description="共享用户列表（仅 owner 可见）")


class KBScopeRequest(BaseModel):
    scope: str = Field(..., pattern="^(private|public)$", description="新的可见范围")


class KBOverviewRequest(BaseModel):
    overview: str


class KBRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class KBTocRequest(BaseModel):
    toc: dict


class KBSummaryRequest(BaseModel):
    summary: str


class QueryKBRequest(BaseModel):
    question: str = Field(..., description="用户提问")
    top_k: int = Field(default=5, ge=1, description="返回相关文档数量")


def _get_current_user(token: str, db=None) -> dict | None:
    if db is None:
        db = default_user_db
    return resolve_principal(db, f"Bearer {token}", None)


async def _require_auth(authorization: str, db=None) -> dict:
    if db is None:
        db = default_user_db
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证信息")
    user_dict = await asyncio.to_thread(resolve_principal, db, authorization, None)
    if not user_dict:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return user_dict


def _check_kb_edit_permission(kb_id: str, user_dict: dict, db=None) -> None:
    if db is None:
        db = default_user_db
    if is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        require_kb_edit(db, kb_id, user_dict)
        return
    meta = db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if user_dict.get("is_admin"):
        return
    if meta["owner_id"] == user_dict["id"]:
        return
    if db.is_kb_shared(kb_id, user_dict["id"], permission="edit"):
        return
    raise HTTPException(status_code=403, detail="仅知识库所有者、管理员或共享编辑者可操作")


def _audit_org_event(user: dict, action: str, kb_id: str, reason: str = "authorized", db=None) -> None:
    if db is None:
        db = default_user_db
    if is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        OrgAuditService(db).record(
            int(user["id"]), action, "knowledge_base", kb_id,
            "success", reason,
        )


def _versions_root(request: Request | None = None) -> Path:
    if request is not None and hasattr(getattr(request, "app", None), "state"):
        return Path(getattr(request.app.state, "document_versions_root", settings.document_versions_root))
    return Path(settings.document_versions_root)


@router.get("/knowledge-bases", summary="列出知识库", description="获取所有知识库列表")
async def list_knowledge_bases(request: Request, user: dict = Depends(require_user)):
    user_db = get_user_db(request)
    if OrgRepository(user_db).get_person_for_user(int(user["id"])) is not None:
        return await asyncio.to_thread(OrgAuthorizationService(user_db).visible_knowledge_bases, int(user["id"]))
    manager = KnowledgeBaseManager()
    result = []
    for kb in await asyncio.to_thread(manager.list_kbs):
        try:
            meta = await asyncio.to_thread(require_kb_view, user_db, kb.kb_id, user)
        except HTTPException as exc:
            if exc.status_code in {403, 409}:
                continue
            raise
        is_owner = meta["owner_id"] == user["id"]
        shares = []
        if is_owner or user.get("is_admin"):
            shares = await asyncio.to_thread(user_db.list_kb_shared_users, kb.kb_id)
        result.append(KBResponse(
            kb_id=kb.kb_id,
            name=meta.get("name", kb.name),
            doc_count=kb.doc_count,
            scope=meta["scope"],
            is_owner=is_owner,
            shared_users=[
                {"username": share["username"], "permission": share["permission"]}
                for share in shares
            ],
        ))
    return result


@router.post("/knowledge-bases", summary="创建知识库", description="创建一个新的知识库")
async def create_knowledge_base(req: CreateKBRequest, request: Request, user: dict = Depends(require_user)):
    user_db = get_user_db(request)
    org_repo = OrgRepository(user_db)
    if org_repo.get_person_for_user(int(user["id"])) is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    if not req.org_unit_code or not OrgAuthorizationService(user_db).can_manage_org(int(user["id"]), req.org_unit_code):
        raise HTTPException(status_code=404, detail="资源不存在")

    def _create_org_kb():
        manager = KnowledgeBaseManager()
        kb_id = manager.create_kb(req.name)
        with user_db.locked_transaction() as conn:
            conn.execute("INSERT INTO kb_metadata(kb_id,name,owner_id,scope) VALUES(?,?,0,'private')", (kb_id, req.name))
            conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES(?,?,'organization')", (kb_id, req.org_unit_code))
        return kb_id

    kb_id = await asyncio.to_thread(_create_org_kb)
    await asyncio.to_thread(_audit_org_event, user, "kb_created", kb_id, "authorized", user_db)
    return {"kb_id": kb_id, "name": req.name, "kb_type": "organization", "org_unit_code": req.org_unit_code, "effective_role": "manage"}


@router.put("/knowledge-bases/{kb_id}/scope", summary="切换知识库可见范围")
async def update_kb_scope(
    kb_id: str,
    req: KBScopeRequest,
    request: Request,
    user: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    if is_org_policy(OrgRepository(user_db).get_kb_policy(kb_id)):
        raise HTTPException(status_code=404, detail="资源不存在")
    require_kb_edit(user_db, kb_id, user)
    if req.scope == "private" and await asyncio.to_thread(user_db.list_kb_shared_users, kb_id):
        raise HTTPException(status_code=409, detail="请先取消所有共享后再设为私有")

    try:
        await asyncio.to_thread(user_db.update_kb_scope, kb_id, req.scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"kb_id": kb_id, "scope": req.scope}


def _resolve_kb_files_root(request: Request | None = None) -> Path:
    if request is not None and hasattr(getattr(request, "app", None), "state"):
        root = getattr(request.app.state, "kb_files_root", None)
        if root is not None:
            return Path(root)
    return KB_FILES_ROOT


@router.delete("/knowledge-bases/{kb_id}", summary="删除知识库", description="删除指定知识库及其所有文档")
async def delete_knowledge_base(kb_id: str, request: Request, user: dict = Depends(require_user)):
    if kb_id == "rag_docs":
        raise HTTPException(status_code=400, detail="不能删除系统集合 rag_docs")

    user_db = get_user_db(request)
    require_kb_administer(user_db, kb_id, user)
    kb_files_root = _resolve_kb_files_root(request)

    manager = KnowledgeBaseManager()
    try:
        documents = await asyncio.to_thread(user_db.list_kb_documents, kb_id)
        for record in documents:
            await asyncio.to_thread(manager.remove_document, kb_id, record["filename"])
            permission = await asyncio.to_thread(
                user_db.get_document_permission, record["filename"], kb_id
            )
            if permission:
                await asyncio.to_thread(user_db.delete_document_permission, permission["id"])
            if record["file_path"]:
                stored = resolve_stored_path(kb_files_root, record["file_path"])
                stored.unlink(missing_ok=True)
                with suppress(OSError):
                    stored.parent.rmdir()
            await asyncio.to_thread(user_db.delete_kb_document, record["id"])
        manager.delete_kb(kb_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在") from exc
    await asyncio.to_thread(_audit_org_event, user, "kb_deleted", kb_id, "authorized", user_db)

    def _cleanup():
        with user_db.locked_transaction() as conn:
            conn.execute("DELETE FROM kb_documents WHERE kb_id = ?", (kb_id,))
            conn.execute("DELETE FROM kb_org_policies WHERE kb_id = ?", (kb_id,))
            conn.execute("DELETE FROM kb_metadata WHERE kb_id = ?", (kb_id,))
    await asyncio.to_thread(_cleanup)
    return {"status": "deleted", "kb_id": kb_id}


@router.post("/knowledge-bases/{kb_id}/documents", summary="添加文档到知识库", description="上传文档到指定知识库")
async def add_document_to_kb(
    kb_id: str,
    request: Request,
    file: UploadFile = File(...),
    duplicate_action: str = Form("reject"),
    user: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    require_kb_edit(user_db, kb_id, user)

    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 10 MB 限制")
    filename = Path(file.filename or "unnamed.txt").name
    if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的文件格式")
    if duplicate_action not in ("reject", "overwrite", "keep"):
        raise HTTPException(status_code=400, detail="无效的 duplicate_action")

    source = UploadDocumentSource.from_bytes(filename, content)
    if duplicate_action == "overwrite" and user_db.get_document_permission(source.display_name, kb_id):
        require_doc_edit(user_db, source.display_name, kb_id, user)
    manager = KnowledgeBaseManager()
    kb_document_id = None
    try:
        final_name = choose_document_name(user_db, kb_id, source.display_name, duplicate_action)
        existing_kb_doc = await asyncio.to_thread(user_db.get_kb_document, kb_id, final_name)
        if existing_kb_doc is None:
            kb_document_id = await asyncio.to_thread(
                user_db.create_kb_document_pending, kb_id, final_name
            )
        else:
            kb_document_id = existing_kb_doc["id"]
        result = await asyncio.to_thread(
            ingest_document_version,
            user_db,
            manager,
            source,
            kb_id,
            user,
            duplicate_action,
            _versions_root(request),
        )
        version = await asyncio.to_thread(
            user_db.get_document_version_by_id, result.version_id
        )
        parsed = await asyncio.to_thread(load_document, version["file_path"])
        toc = _json.dumps(generate_toc(parsed), ensure_ascii=False)
        summary = generate_summary(parsed)
        await asyncio.to_thread(
            user_db.finish_kb_document,
            kb_document_id,
            version["file_path"],
            toc,
            summary,
            result.chunks,
        )
        if Path(result.filename).suffix.lower() in {".xlsx", ".csv"}:
            spreadsheet_format = Path(result.filename).suffix.lower().removeprefix(".")
            try:
                datasets_root = Path(
                    getattr(
                        request.app.state,
                        "structured_datasets_root",
                        settings.structured_datasets_root,
                    )
                ) if hasattr(getattr(request, "app", None), "state") else Path(settings.structured_datasets_root)
                await asyncio.to_thread(
                    ensure_structured_dataset,
                    user_db,
                    datasets_root,
                    document_id=result.document_id,
                    version_id=result.version_id,
                    source_path=Path(version["file_path"]),
                    fmt=spreadsheet_format,
                )
            except Exception:
                logger.exception(
                    "KB spreadsheet dataset extraction failed",
                    extra={"kb_id": kb_id, "document_id": result.document_id},
                )
    except DuplicateDocument as exc:
        if kb_document_id is not None:
            await asyncio.to_thread(user_db.delete_kb_document, kb_document_id)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_document",
                "document_id": exc.document_id,
                "current_version": exc.current_version,
            },
        ) from exc
    except HTTPException:
        if kb_document_id is not None:
            await asyncio.to_thread(user_db.delete_kb_document, kb_document_id)
        raise
    except Exception as exc:
        if kb_document_id is not None:
            await asyncio.to_thread(user_db.delete_kb_document, kb_document_id)
        logger.exception("KB document upload failed", extra={"kb_id": kb_id})
        raise HTTPException(status_code=400, detail="文件处理失败") from exc

    await asyncio.to_thread(_audit_org_event, user, "document_uploaded", kb_id, "authorized", user_db)
    return {
        "status": "added",
        "kb_id": kb_id,
        "chunks": result.chunks,
        "document_id": result.document_id,
        "version_id": result.version_id,
        "version_no": result.version_no,
    }


@router.delete("/knowledge-bases/{kb_id}/documents/{doc_name}", summary="删除文档", description="从知识库中删除指定文档")
async def remove_document_from_kb(
    kb_id: str, doc_name: str, request: Request, user: dict = Depends(require_user)
):
    user_db = get_user_db(request)
    require_kb_edit(user_db, kb_id, user)
    record = await asyncio.to_thread(user_db.get_kb_document, kb_id, doc_name)
    if record is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_doc_edit(user_db, doc_name, kb_id, user)

    manager = KnowledgeBaseManager()
    try:
        await asyncio.to_thread(user_db.set_kb_document_state, record["id"], "deleting")
        await asyncio.to_thread(manager.remove_document, kb_id, doc_name)
        permission = await asyncio.to_thread(user_db.get_document_permission, doc_name, kb_id)
        if permission:
            await asyncio.to_thread(user_db.delete_document_permission, permission["id"])
        if record["file_path"]:
            stored = resolve_stored_path(_resolve_kb_files_root(request), record["file_path"])
            stored.unlink(missing_ok=True)
            with suppress(OSError):
                stored.parent.rmdir()
        await asyncio.to_thread(user_db.delete_kb_document, record["id"])
    except Exception as exc:
        await asyncio.to_thread(
            user_db.set_kb_document_state, record["id"], "delete_failed", str(exc)
        )
        logger.exception("KB document deletion failed", extra={"kb_id": kb_id})
        raise HTTPException(status_code=400, detail="文档删除失败") from exc

    await asyncio.to_thread(_audit_org_event, user, "document_deleted", kb_id, "authorized", user_db)
    return {"status": "removed", "kb_id": kb_id, "doc_name": doc_name}


@router.post("/knowledge-bases/{kb_id}/query", summary="查询知识库", description="对指定知识库进行语义检索")
async def query_knowledge_base(
    kb_id: str, req: QueryKBRequest, request: Request, user: dict = Depends(require_user)
):
    user_db = get_user_db(request)
    require_kb_view(user_db, kb_id, user)

    manager = KnowledgeBaseManager()
    try:
        chunks = manager.search(kb_id, req.question, top_k=req.top_k * 3)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    visible_chunks = await asyncio.to_thread(
        filter_chunks_by_permission, user_db, kb_id, chunks, user
    )
    results = [
        {"doc_name": c.doc_name, "chunk_index": c.chunk_index, "text": c.text}
        for c in visible_chunks[:req.top_k]
    ]
    await asyncio.to_thread(_audit_org_event, user, "kb_queried", kb_id, "authorized", user_db)
    return {"kb_id": kb_id, "results": results}


@router.get("/knowledge-bases/{kb_id}", summary="获取知识库详情")
async def get_knowledge_base_detail(
    kb_id: str,
    request: Request,
    user: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    meta = require_kb_view(user_db, kb_id, user)

    def _get():
        with user_db.locked_read() as conn:
            db_meta = conn.execute("SELECT * FROM kb_metadata WHERE kb_id = ?", (kb_id,)).fetchone()
            if db_meta is None:
                return None
            docs = conn.execute(
                "SELECT id, kb_id, filename, toc, summary, chunk_count, status, added_at, "
                "document_id, document_version_id "
                "FROM kb_documents WHERE kb_id = ? ORDER BY added_at DESC",
                (kb_id,),
            ).fetchall()
            return {
                "kb_id": kb_id,
                "name": meta.get("name", db_meta["name"]),
                "description": db_meta["description"] or "",
                "overview": db_meta["overview"] or "",
                "doc_count": len(docs),
                "kb_type": meta.get("kb_type"),
                "org_unit_code": meta.get("org_code"),
                "effective_role": meta.get("effective_role", "administer" if user.get("is_admin") else "read"),
                "sensitivity": meta.get("sensitivity"),
                "documents": [dict(d) for d in docs],
            }

    result = await asyncio.to_thread(_get)
    if not result:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return result


@router.put("/knowledge-bases/{kb_id}/overview", summary="更新知识库概述")
async def update_kb_overview(
    kb_id: str, req: KBOverviewRequest, request: Request, user: dict = Depends(require_user)
):
    user_db = get_user_db(request)
    require_kb_edit(user_db, kb_id, user)
    updated = await asyncio.to_thread(user_db.update_kb_overview, kb_id, req.overview)
    if not updated:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"status": "updated"}


@router.put("/knowledge-bases/{kb_id}/name", summary="重命名知识库")
async def rename_knowledge_base(
    kb_id: str,
    req: KBRenameRequest,
    request: Request,
    user_dict: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    _check_kb_edit_permission(kb_id, user_dict, user_db)

    def _update():
        with user_db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO kb_metadata (kb_id, name) VALUES (?, ?) "
                "ON CONFLICT(kb_id) DO UPDATE SET name = ?",
                (kb_id, req.name, req.name),
            )

    await asyncio.to_thread(_update)
    await asyncio.to_thread(_audit_org_event, user_dict, "kb_renamed", kb_id, "authorized", user_db)
    return {"status": "updated", "name": req.name}


@router.put("/knowledge-bases/{kb_id}/documents/{doc_name}/toc", summary="更新文档目录")
async def update_doc_toc(
    kb_id: str, doc_name: str, req: KBTocRequest, request: Request, user: dict = Depends(require_user)
):
    user_db = get_user_db(request)
    document = await asyncio.to_thread(user_db.get_kb_document, kb_id, doc_name)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_doc_edit(user_db, doc_name, kb_id, user)

    def _update():
        toc_str = _json.dumps(req.toc, ensure_ascii=False)
        return user_db.update_kb_document_toc(kb_id, doc_name, toc_str)

    result = await asyncio.to_thread(_update)
    if not result:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"status": "updated"}


@router.put("/knowledge-bases/{kb_id}/documents/{doc_name}/summary", summary="更新文档概述")
async def update_doc_summary(
    kb_id: str, doc_name: str, req: KBSummaryRequest, request: Request, user: dict = Depends(require_user)
):
    user_db = get_user_db(request)
    document = await asyncio.to_thread(user_db.get_kb_document, kb_id, doc_name)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_doc_edit(user_db, doc_name, kb_id, user)

    await asyncio.to_thread(user_db.update_kb_document_summary, kb_id, doc_name, req.summary)
    return {"status": "updated"}


@router.post("/knowledge-bases/{kb_id}/toc/generate", summary="LLM 生成知识库目录")
async def generate_kb_toc(
    kb_id: str,
    request: Request,
    user_dict: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    _check_kb_edit_permission(kb_id, user_dict, user_db)

    kb_files_root = _resolve_kb_files_root(request)

    def _generate():
        try:
            with user_db.locked_read() as conn:
                docs = conn.execute(
                    "SELECT filename,file_path FROM kb_documents WHERE kb_id = ? AND status = 'indexed'",
                    (kb_id,),
                ).fetchall()
            if not docs:
                return None, "no_documents"

            toc_results = {}
            for doc in docs:
                filename = doc["filename"]
                stored_path = Path(doc["file_path"]) if doc["file_path"] else DATA_DIR / filename
                file_path = stored_path if stored_path.is_absolute() else resolve_stored_path(kb_files_root, str(stored_path))
                if file_path.is_file():
                    try:
                        content = load_document(str(file_path))
                    except Exception:
                        content = ""
                else:
                    content = ""
                toc = generate_toc(content)
                toc_results[filename] = toc
                toc_str = _json.dumps(toc, ensure_ascii=False)
                with user_db.locked_transaction() as conn:
                    conn.execute(
                        "UPDATE kb_documents SET toc = ? WHERE kb_id = ? AND filename = ?",
                        (toc_str, kb_id, filename),
                    )
            return toc_results, None
        except Exception as e:
            return None, str(e)

    result, error = await asyncio.to_thread(_generate)
    if error == "not_found":
        raise HTTPException(status_code=404, detail="知识库不存在")
    if error == "no_documents":
        raise HTTPException(status_code=400, detail="知识库中无文档，请先添加文档")
    if error:
        raise HTTPException(status_code=500, detail=f"目录生成失败: {error}")
    return {"kb_id": kb_id, "toc": result}


@router.post("/knowledge-bases/{kb_id}/overview/generate", summary="LLM 生成知识库概述")
async def generate_kb_overview(
    kb_id: str,
    request: Request,
    user_dict: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    _check_kb_edit_permission(kb_id, user_dict, user_db)
    kb_files_root = _resolve_kb_files_root(request)

    def _generate():
        try:
            with user_db.locked_read() as conn:
                docs = conn.execute(
                    "SELECT filename,file_path FROM kb_documents WHERE kb_id = ? AND status = 'indexed'",
                    (kb_id,),
                ).fetchall()
            if not docs:
                return None, "no_documents"

            all_content = []
            for doc in docs:
                filename = doc["filename"]
                stored_path = Path(doc["file_path"]) if doc["file_path"] else DATA_DIR / filename
                file_path = stored_path if stored_path.is_absolute() else resolve_stored_path(kb_files_root, str(stored_path))
                if file_path.is_file():
                    try:
                        content = load_document(str(file_path))
                    except Exception:
                        content = ""
                    all_content.append(content)

            combined = "\n\n".join(all_content)
            overview = generate_summary(combined)

            with user_db.locked_transaction() as conn:
                conn.execute(
                    "INSERT INTO kb_metadata (kb_id, name, overview) VALUES (?, '', ?) "
                    "ON CONFLICT(kb_id) DO UPDATE SET overview = ?",
                    (kb_id, overview, overview),
                )
            return overview, None
        except Exception as e:
            return None, str(e)

    result, error = await asyncio.to_thread(_generate)
    if error == "not_found":
        raise HTTPException(status_code=404, detail="知识库不存在")
    if error == "no_documents":
        raise HTTPException(status_code=400, detail="知识库中无文档，请先添加文档")
    if error:
        raise HTTPException(status_code=500, detail=f"概述生成失败: {error}")
    return {"kb_id": kb_id, "overview": result}


@router.post("/knowledge-bases/{kb_id}/share", summary="共享知识库给指定用户")
async def share_knowledge_base(
    kb_id: str,
    req: ShareRequest,
    request: Request,
    user_dict: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    if is_org_policy(OrgRepository(user_db).get_kb_policy(kb_id)):
        raise HTTPException(status_code=404, detail="资源不存在")

    meta = user_db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not user_dict.get("is_admin") and meta["owner_id"] != user_dict["id"]:
        raise HTTPException(status_code=403, detail="仅知识库所有者或管理员可共享")

    target = user_db.get_user_by_id(req.user_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"用户 ID {req.user_id} 不存在")

    try:
        await asyncio.to_thread(user_db.share_kb, kb_id, req.user_id, user_dict["id"], req.permission)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail="该用户已被共享") from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {"kb_id": kb_id, "user_id": req.user_id, "permission": req.permission}


@router.delete("/knowledge-bases/{kb_id}/share/{user_id}", summary="取消知识库共享", status_code=204)
async def unshare_knowledge_base(
    kb_id: str,
    user_id: int,
    request: Request,
    user_dict: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    if is_org_policy(OrgRepository(user_db).get_kb_policy(kb_id)):
        raise HTTPException(status_code=404, detail="资源不存在")

    meta = user_db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not user_dict.get("is_admin") and meta["owner_id"] != user_dict["id"]:
        raise HTTPException(status_code=403, detail="仅知识库所有者或管理员可操作")

    await asyncio.to_thread(user_db.unshare_kb, kb_id, user_id)
    return None


@router.get("/knowledge-bases/{kb_id}/shares", summary="查看知识库共享列表")
async def list_kb_shares(
    kb_id: str,
    request: Request,
    user_dict: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    if is_org_policy(OrgRepository(user_db).get_kb_policy(kb_id)):
        raise HTTPException(status_code=404, detail="资源不存在")

    meta = user_db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not user_dict.get("is_admin") and meta["owner_id"] != user_dict["id"]:
        raise HTTPException(status_code=403, detail="仅知识库所有者或管理员可查看")

    shares = await asyncio.to_thread(user_db.list_kb_shared_users, kb_id)
    return shares
