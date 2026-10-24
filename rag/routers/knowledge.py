"""Knowledge base management routes."""

from __future__ import annotations

import asyncio
import json as _json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from config import settings
from rag.api_security import (
    get_user_db,
    is_org_policy,
    require_doc_edit,
    require_doc_view,
    require_kb_administer,
    require_kb_edit,
    require_kb_view,
    require_user,
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
from rag.kb_document_cleanup import (
    delete_kb_document_files,
    resolve_kb_document_paths,
)
from rag.kb_file_store import resolve_stored_path
from rag.kb_lifecycle import KBLifecycleError, kb_mutation
from rag.kb_lifecycle_cleanup import (
    cleanup_kb_files,
    collect_kb_cleanup,
    finish_kb_cleanup_sql,
)
from rag.kb_lifecycle_http import lifecycle_http_error
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


class LinkDocumentRequest(BaseModel):
    """Reference an existing document into another KB without a browser download."""

    document_id: int = Field(..., ge=1)
    duplicate_action: str = Field(default="reject", pattern="^(reject|overwrite|keep)$")


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


def _check_kb_edit_permission(kb_id: str, user_dict: dict, db=None) -> None:
    """Legacy (non-org) KB edit gate: owner, admin, or a shared editor.

    Denials are reported as 404 so that a caller cannot distinguish "not yours"
    from "does not exist" — the same contract the organization-policy branches
    already use (B-06).
    """
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
    raise HTTPException(status_code=404, detail="资源不存在")


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


def _uses_org_kb_scope(user_db, user: dict) -> bool:
    """Org-scoped KB listing applies only to non-admin org members.

    An admin who also has an ``org_people`` row must keep the global list; otherwise
    legacy KBs (no ``kb_org_policies`` row) disappear from the admin's view.
    """
    if user.get("is_admin"):
        return False
    return OrgRepository(user_db).get_person_for_user(int(user["id"])) is not None


@router.get("/knowledge-bases", summary="列出知识库", description="获取所有知识库列表")
async def list_knowledge_bases(request: Request, user: dict = Depends(require_user)):
    user_db = get_user_db(request)
    if _uses_org_kb_scope(user_db, user):
        return await asyncio.to_thread(OrgAuthorizationService(user_db).visible_knowledge_bases, int(user["id"]))
    manager = KnowledgeBaseManager()
    result = []
    for kb in await asyncio.to_thread(manager.list_kbs):
        try:
            meta = await asyncio.to_thread(require_kb_view, user_db, kb.kb_id, user)
        except HTTPException as exc:
            # 403/404/409 都是"这个库对当前主体不可见"：迁移后的 unassigned 库
            # 会给出 404，之前只 continue 403/409 会让一个未归属的库把整张列表
            # 变成 404（B-06）。跳过它，其余可见库照常返回。
            if exc.status_code in {403, 404, 409}:
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
        from rag.kb_lifecycle import assert_kb_available

        kb_id = manager.create_kb(req.name, before_create=lambda candidate: assert_kb_available(user_db, candidate))
        with user_db.locked_transaction() as conn:
            from rag.kb_lifecycle import assert_kb_available_in_tx

            assert_kb_available_in_tx(conn, kb_id)
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
        def _update_scope():
            if req.scope not in {"private", "public"}:
                raise ValueError(f"无效的 scope: {req.scope}，必须是 'private' 或 'public'")
            with user_db.locked_transaction(immediate=True) as conn:
                from rag.kb_lifecycle import assert_kb_available_in_tx

                assert_kb_available_in_tx(conn, kb_id)
                conn.execute("UPDATE kb_metadata SET scope = ? WHERE kb_id = ?", (req.scope, kb_id))
                conn.execute("DELETE FROM kb_shares WHERE kb_id = ?", (kb_id,))

        await asyncio.to_thread(
            _kb_write_sync, user_db, kb_id, user,
            _update_scope,
        )
    except KBLifecycleError as e:
        raise lifecycle_http_error(e) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"kb_id": kb_id, "scope": req.scope}


def _resolve_kb_files_root(request: Request | None = None) -> Path:
    if request is not None and hasattr(getattr(request, "app", None), "state"):
        root = getattr(request.app.state, "kb_files_root", None)
        if root is not None:
            return Path(root)
    return KB_FILES_ROOT


def _ingest_kb_document_sync(
    source, db, manager, actor, roots, kb_id, duplicate_action, audit_action="document_uploaded"
):
    """Run every durable upload/link mutation under one synchronous KB permit."""
    from rag.kb_lifecycle import kb_mutation

    kb_document_id = None
    created = False
    with kb_mutation(db, kb_id):
        try:
            require_kb_edit(db, kb_id, actor)
            if duplicate_action == "overwrite" and db.get_document_permission(source.display_name, kb_id):
                require_doc_edit(db, source.display_name, kb_id, actor)
            final_name = choose_document_name(db, kb_id, source.display_name, duplicate_action)
            existing = db.get_kb_document(kb_id, final_name)
            if existing is None:
                kb_document_id = db.create_kb_document_pending(kb_id, final_name)
                created = True
            else:
                kb_document_id = existing["id"]
            result = ingest_document_version(
                db, manager, source, kb_id, actor, duplicate_action, Path(roots["versions_root"])
            )
            version = db.get_document_version_by_id(result.version_id)
            parsed = load_document(version["file_path"])
            toc = _json.dumps(generate_toc(parsed), ensure_ascii=False)
            summary = generate_summary(parsed)
            db.finish_kb_document(
                kb_document_id, version["file_path"], toc, summary, result.chunks
            )
            if Path(result.filename).suffix.lower() in {".xlsx", ".csv"}:
                try:
                    ensure_structured_dataset(
                        db,
                        Path(roots["datasets_root"]),
                        document_id=result.document_id,
                        version_id=result.version_id,
                        source_path=Path(version["file_path"]),
                        fmt=Path(result.filename).suffix.lower().removeprefix("."),
                    )
                except Exception:
                    logger.exception(
                        "KB spreadsheet dataset extraction failed",
                        extra={"kb_id": kb_id, "document_id": result.document_id},
                    )
            _audit_org_event(actor, audit_action, kb_id, "authorized", db)
            return result
        except BaseException:
            if created and kb_document_id is not None:
                db.delete_kb_document(kb_document_id)
            raise


def _kb_write_sync(db, kb_id: str, actor: dict, callback):
    """Run a metadata/directory write behind the same synchronous lifecycle gate."""
    with kb_mutation(db, kb_id):
        require_kb_edit(db, kb_id, actor)
        return callback()


@router.delete("/knowledge-bases/{kb_id}", summary="删除知识库", description="删除指定知识库及其所有文档")
async def delete_knowledge_base(kb_id: str, request: Request, user: dict = Depends(require_user)):
    if kb_id == "rag_docs":
        raise HTTPException(status_code=400, detail="不能删除系统集合 rag_docs")

    user_db = get_user_db(request)
    require_kb_administer(user_db, kb_id, user, allow_deleting=True)
    versions_root = _versions_root(request)
    kb_files_root = _resolve_kb_files_root(request)
    datasets_root = Path(getattr(getattr(request, "app", None).state, "structured_datasets_root", settings.structured_datasets_root)) if getattr(request, "app", None) is not None else Path(settings.structured_datasets_root)

    def _delete_worker():
        with kb_mutation(user_db, kb_id, mode="delete") as permit:
            require_kb_administer(user_db, kb_id, user, allow_deleting=True)
            permit.begin_delete()
            try:
                permit.assert_delete_started()
                plan = collect_kb_cleanup(
                    user_db, kb_id, versions_root, kb_files_root, datasets_root
                )
                permission_ids = [int(row["id"]) for row in plan["permissions"]]
                user_db.cancel_material_jobs_for_documents(permission_ids)

                from rag.wiki.api import get_wiki_indexer, get_wiki_store

                wiki_store = get_wiki_store()
                with wiki_store._lock:
                    page_ids = [
                        int(row[0])
                        for row in wiki_store._conn.execute(
                            "SELECT id FROM wiki_pages WHERE kb_id = ?", (kb_id,)
                        ).fetchall()
                    ]
                user_db.cancel_wiki_reindex_jobs(page_ids)

                manager = KnowledgeBaseManager()
                for record in plan["documents"]:
                    manager.remove_document(kb_id, record["filename"])
                from rag.bm25_store import BM25Store

                bm25 = BM25Store()
                try:
                    bm25.invalidate(kb_id)
                finally:
                    bm25.close()
                get_wiki_indexer(user_db).remove_kb_content(kb_id, db=user_db)
                manager.delete_kb(kb_id)
                cleanup_kb_files(plan)
                _audit_org_event(user, "kb_deleted", kb_id, "authorized", user_db)
                permit.finish_delete(lambda conn: finish_kb_cleanup_sql(conn, kb_id, plan))
            except Exception as exc:
                try:
                    permit.fail_delete(getattr(exc, "code", "cleanup_failed"))
                except Exception:
                    logger.exception("Failed to record KB deletion failure", extra={"kb_id": kb_id})
                raise

    try:
        await asyncio.to_thread(_delete_worker)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("KB deletion cleanup failed", extra={"kb_id": kb_id})
        raise HTTPException(status_code=503, detail="知识库删除未完成，请重试。", headers={"X-KB-Lifecycle-Error": "knowledge_base_delete_incomplete"}) from exc
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
    roots = {
        "versions_root": _versions_root(request),
        "datasets_root": Path(getattr(request.app.state, "structured_datasets_root", settings.structured_datasets_root))
        if hasattr(getattr(request, "app", None), "state") else Path(settings.structured_datasets_root),
    }
    try:
        result = await asyncio.to_thread(
            _ingest_kb_document_sync,
            source, user_db, KnowledgeBaseManager(), user, roots, kb_id, duplicate_action,
        )
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    except DuplicateDocument as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_document",
                "document_id": exc.document_id,
                "current_version": exc.current_version,
            },
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("KB document upload failed", extra={"kb_id": kb_id})
        raise HTTPException(status_code=400, detail="文件处理失败") from exc
    return {
        "status": "added",
        "kb_id": kb_id,
        "chunks": result.chunks,
        "document_id": result.document_id,
        "version_id": result.version_id,
        "version_no": result.version_no,
    }


@router.post(
    "/knowledge-bases/{kb_id}/documents/link",
    summary="引用已有文档到知识库",
    description="从已有 document_id 读取源文件并在目标知识库重新索引，不经过浏览器下载再上传",
)
async def link_document_to_kb(
    kb_id: str,
    body: LinkDocumentRequest,
    request: Request,
    user: dict = Depends(require_user),
):
    user_db = get_user_db(request)
    require_kb_edit(user_db, kb_id, user)
    source_identity = user_db.get_document_permission_by_id(body.document_id)
    if source_identity is None:
        raise HTTPException(status_code=404, detail="资源不存在")

    def _source_snapshot():
        with kb_mutation(user_db, source_identity["kb_id"]):
            source_doc = user_db.get_document_permission_by_id(body.document_id)
            if source_doc is None:
                raise HTTPException(status_code=404, detail="资源不存在")
            require_doc_view(user_db, source_doc["doc_name"], source_doc["kb_id"], user)
            version_id = source_doc.get("current_version_id")
            version = user_db.get_document_version_by_id(version_id) if version_id else None
            if version is None:
                raise HTTPException(status_code=409, detail="源文档没有可用版本")
            source_path = Path(version["file_path"])
            if not source_path.is_file():
                raise HTTPException(status_code=409, detail="源文档文件不可用")
            content = source_path.read_bytes()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="文件超过 10 MB 限制")
            filename = Path(source_doc["doc_name"]).name
            if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise HTTPException(status_code=400, detail="不支持的文件格式")
            return UploadDocumentSource.from_bytes(filename, content)

    try:
        source = await asyncio.to_thread(_source_snapshot)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    roots = {
        "versions_root": _versions_root(request),
        "datasets_root": Path(getattr(request.app.state, "structured_datasets_root", settings.structured_datasets_root))
        if hasattr(getattr(request, "app", None), "state") else Path(settings.structured_datasets_root),
    }
    try:
        result = await asyncio.to_thread(
            _ingest_kb_document_sync,
            source, user_db, KnowledgeBaseManager(), user, roots, kb_id,
            body.duplicate_action, "document_linked",
        )
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    except DuplicateDocument as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_document",
                "document_id": exc.document_id,
                "current_version": exc.current_version,
            },
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("KB document link failed", extra={"kb_id": kb_id})
        raise HTTPException(status_code=400, detail="文件处理失败") from exc
    return {
        "status": "linked",
        "kb_id": kb_id,
        "source_document_id": body.document_id,
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
    def _delete_document():
        with kb_mutation(user_db, kb_id):
            require_kb_edit(user_db, kb_id, user)
            record = user_db.get_kb_document(kb_id, doc_name)
            if record is None:
                raise HTTPException(status_code=404, detail="文档不存在")
            require_doc_edit(user_db, doc_name, kb_id, user)
            paths = resolve_kb_document_paths(
                user_db, kb_id, record, _versions_root(request), _resolve_kb_files_root(request)
            )
            try:
                user_db.set_kb_document_state(record["id"], "deleting")
                KnowledgeBaseManager().remove_document(kb_id, doc_name)
                delete_kb_document_files(paths)
                permission = user_db.get_document_permission(doc_name, kb_id)
                if permission:
                    user_db.delete_document_permission(permission["id"])
                user_db.delete_kb_document(record["id"])
                _audit_org_event(user, "document_deleted", kb_id, "authorized", user_db)
            except Exception as exc:
                user_db.set_kb_document_state(record["id"], "delete_failed", str(exc))
                raise

    try:
        await asyncio.to_thread(_delete_document)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    except Exception as exc:
        logger.exception("KB document deletion failed", extra={"kb_id": kb_id})
        raise HTTPException(status_code=400, detail="文档删除失败") from exc
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
    except Exception as exc:
        logger.exception("KB query failed", extra={"kb_id": kb_id})
        # 内部异常文本不回显（B-05）。
        raise HTTPException(status_code=400, detail="知识库检索失败") from exc
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
    try:
        updated = await asyncio.to_thread(
            _kb_write_sync, user_db, kb_id, user,
            lambda: user_db.update_kb_overview(kb_id, req.overview),
        )
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
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
        with kb_mutation(user_db, kb_id):
            require_kb_edit(user_db, kb_id, user_dict)
            return _update_in_tx()

    def _update_in_tx():
        with user_db.locked_transaction() as conn:
            from rag.kb_lifecycle import assert_kb_available_in_tx

            assert_kb_available_in_tx(conn, kb_id)
            conn.execute(
                "INSERT INTO kb_metadata (kb_id, name) VALUES (?, ?) "
                "ON CONFLICT(kb_id) DO UPDATE SET name = ?",
                (kb_id, req.name, req.name),
            )

    try:
        await asyncio.to_thread(_update)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
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
        with kb_mutation(user_db, kb_id):
            require_doc_edit(user_db, doc_name, kb_id, user)
            toc_str = _json.dumps(req.toc, ensure_ascii=False)
            return user_db.update_kb_document_toc(kb_id, doc_name, toc_str)

    try:
        result = await asyncio.to_thread(_update)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
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

    try:
        def _update_summary():
            with kb_mutation(user_db, kb_id):
                require_doc_edit(user_db, doc_name, kb_id, user)
                with user_db.locked_transaction(immediate=True) as conn:
                    from rag.kb_lifecycle import assert_kb_available_in_tx

                    assert_kb_available_in_tx(conn, kb_id)
                    cursor = conn.execute(
                        "UPDATE kb_documents SET summary = ? WHERE kb_id = ? AND filename = ?",
                        (req.summary, kb_id, doc_name),
                    )
                    return cursor.rowcount > 0

        await asyncio.to_thread(
            _update_summary,
        )
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
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
                    from rag.kb_lifecycle import assert_kb_available_in_tx

                    assert_kb_available_in_tx(conn, kb_id)
                    conn.execute(
                        "UPDATE kb_documents SET toc = ? WHERE kb_id = ? AND filename = ?",
                        (toc_str, kb_id, filename),
                    )
            return toc_results, None
        except KBLifecycleError:
            raise
        except Exception:
            logger.exception("KB toc generation failed", extra={"kb_id": kb_id})
            # 返回使用固定标记而非异常文本：调用方只把它翻译成不透明错误（B-05）。
            return None, "internal_error"

    def _generate_under_gate():
        with kb_mutation(user_db, kb_id):
            _check_kb_edit_permission(kb_id, user_dict, user_db)
            return _generate()

    try:
        result, error = await asyncio.to_thread(_generate_under_gate)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    if error == "no_documents":
        raise HTTPException(status_code=400, detail="知识库中无文档，请先添加文档")
    if error:
        raise HTTPException(status_code=500, detail="目录生成失败")
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
                from rag.kb_lifecycle import assert_kb_available_in_tx

                assert_kb_available_in_tx(conn, kb_id)
                conn.execute(
                    "INSERT INTO kb_metadata (kb_id, name, overview) VALUES (?, '', ?) "
                    "ON CONFLICT(kb_id) DO UPDATE SET overview = ?",
                    (kb_id, overview, overview),
                )
            return overview, None
        except KBLifecycleError:
            raise
        except Exception:
            logger.exception("KB overview generation failed", extra={"kb_id": kb_id})
            # 固定标记，异常文本只进日志（B-05）。
            return None, "internal_error"

    def _generate_under_gate():
        with kb_mutation(user_db, kb_id):
            _check_kb_edit_permission(kb_id, user_dict, user_db)
            return _generate()

    try:
        result, error = await asyncio.to_thread(_generate_under_gate)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    if error == "no_documents":
        raise HTTPException(status_code=400, detail="知识库中无文档，请先添加文档")
    if error:
        raise HTTPException(status_code=500, detail="概述生成失败")
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
        # 与组织库分支一致：非所有者看不到"存在但无权"（B-06）。
        raise HTTPException(status_code=404, detail="资源不存在")

    target = user_db.get_user_by_id(req.user_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"用户 ID {req.user_id} 不存在")

    try:
        def _share():
            with kb_mutation(user_db, kb_id):
                require_kb_edit(user_db, kb_id, user_dict)
                with user_db.locked_transaction(immediate=True) as conn:
                    from rag.kb_lifecycle import assert_kb_available_in_tx

                    assert_kb_available_in_tx(conn, kb_id)
                    cursor = conn.execute(
                        "INSERT INTO kb_shares (kb_id, user_id, permission, granted_by) "
                        "VALUES (?, ?, ?, ?)",
                        (kb_id, req.user_id, req.permission, user_dict["id"]),
                    )
                    conn.execute(
                        "UPDATE kb_metadata SET scope = 'shared' WHERE kb_id = ? AND scope != 'shared'",
                        (kb_id,),
                    )
                    return int(cursor.lastrowid)

        await asyncio.to_thread(
            _share,
        )
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="该用户已被共享") from exc
        logger.exception("KB share failed", extra={"kb_id": kb_id, "user_id": req.user_id})
        # 数据库内部错误文本不回显（B-05）。
        raise HTTPException(status_code=500, detail="共享知识库失败") from exc

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
        raise HTTPException(status_code=404, detail="资源不存在")

    try:
        def _unshare():
            with kb_mutation(user_db, kb_id):
                require_kb_edit(user_db, kb_id, user_dict)
                with user_db.locked_transaction(immediate=True) as conn:
                    from rag.kb_lifecycle import assert_kb_available_in_tx

                    assert_kb_available_in_tx(conn, kb_id)
                    conn.execute("DELETE FROM kb_shares WHERE kb_id = ? AND user_id = ?", (kb_id, user_id))
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM kb_shares WHERE kb_id = ?", (kb_id,)
                    ).fetchone()[0]
                    if remaining == 0:
                        conn.execute(
                            "UPDATE kb_metadata SET scope = 'private' WHERE kb_id = ? AND scope = 'shared'",
                            (kb_id,),
                        )

        await asyncio.to_thread(
            _unshare,
        )
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
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
        raise HTTPException(status_code=404, detail="资源不存在")

    shares = await asyncio.to_thread(user_db.list_kb_shared_users, kb_id)
    return shares
