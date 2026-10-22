"""Document upload, indexing, permissions, and file management routes."""

from __future__ import annotations

import asyncio
import json as _json
import logging
import sqlite3 as _sqlite3
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from qdrant_client.models import FieldCondition, Filter, MatchValue

import rag.permissions as perm_mod
import rag.vector_store as vector_store
from config import settings
from rag.api_security import (
    get_user_db,
    optional_user,
    require_admin,
    require_doc_edit,
    require_personal_document_acl,
    require_user,
)
from rag.batch_importer import BatchImporter
from rag.context import (
    DATA_DIR as DATA_DIR,
)
from rag.context import (
    MAX_FILE_SIZE as MAX_FILE_SIZE,
)
from rag.context import (
    SUPPORTED_EXTENSIONS as SUPPORTED_EXTENSIONS,
)
from rag.context import (
    _get_or_create_pipeline as _get_or_create_pipeline,
)
from rag.context import (
    _get_repo_file_names as _get_repo_file_names,
)
from rag.context import (
    _make_pipeline as _make_pipeline,
)
from rag.context import (
    get_pipeline as get_pipeline,
)
from rag.context import (
    get_pipeline_lock as get_pipeline_lock,
)
from rag.context import (
    get_upload_dir as get_upload_dir,
)
from rag.context import (
    set_pipeline as set_pipeline,
)
from rag.document_sources.upload import UploadDocumentSource
from rag.document_version_store import resolve_version_path
from rag.document_versions import (
    DuplicateDocument,
    ingest_document_version,
)
from rag.folder_indexer import index_folder
from rag.knowledge_base import KnowledgeBaseManager
from rag.user_db import UserDB
from rag.vector_store import COLLECTION_NAME, delete_doc

logger = logging.getLogger(__name__)

router = APIRouter()


class ShareRequest(BaseModel):
    user_id: int = Field(..., description="被共享用户 ID")
    permission: str = Field(default="view", pattern="^(view|edit)$", description="权限：view 或 edit")


def _get_kb_manager() -> KnowledgeBaseManager:
    return KnowledgeBaseManager()


def _get_upload_dir(request: Request | None = None) -> Path:
    return get_upload_dir(request)


def _get_versions_root(request: Request | None = None) -> Path:
    if request is not None and hasattr(getattr(request, "app", None), "state"):
        return Path(getattr(request.app.state, "document_versions_root", settings.document_versions_root))
    return Path(settings.document_versions_root)


def _human_size(size: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _check_files_in_kb(filenames: list[str], db: UserDB | None = None) -> set[str]:
    if not filenames:
        return set()
    if db is None:
        db = get_user_db()
    with db.locked_read() as conn:
        placeholders = ",".join("?" * len(filenames))
        rows = conn.execute(
            f"SELECT DISTINCT filename FROM kb_documents WHERE filename IN ({placeholders}) AND status = 'indexed'",
            filenames,
        ).fetchall()
        return {r[0] for r in rows}


def _can_overwrite_existing(user: dict | None, existing_perm: dict | None) -> bool:
    """同名覆盖保护：目标文件已存在时，仅 owner 或公开文件可被覆盖。

    - admin：可覆盖任意文件
    - owner（权限记录 owner_id == user.id）：可覆盖
    - 公开文件（scope='public'）：可覆盖
    - 无权限记录的既有文件（非 admin、有认证身份）：禁止覆盖
    - 认证关闭下的匿名上传（user 为 None）：仅可覆盖同样无权限记录的既有文件
      （保持认证关闭时的匿名覆盖兼容；不得覆盖有归属/公开记录的文件）
    """
    if user is None:
        return existing_perm is None
    if user.get("is_admin"):
        return True
    if existing_perm is None:
        return False
    if existing_perm["owner_id"] == user["id"]:
        return True
    return existing_perm.get("scope") == "public"


async def _ingest_default_document(
    *,
    content: bytes,
    filename: str,
    user: dict,
    db: UserDB,
    versions_root: Path | None = None,
    duplicate_action: str = "reject",
) -> dict:
    filename = Path(filename or "upload.txt").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的文件格式")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 10 MB 限制")
    if duplicate_action not in ("reject", "overwrite", "keep"):
        raise HTTPException(status_code=400, detail="无效的 duplicate_action")

    if versions_root is None:
        versions_root = _get_versions_root()

    source = UploadDocumentSource.from_bytes(filename, content)
    if duplicate_action == "overwrite" and db.get_document_permission(source.display_name, "rag_docs"):
        require_doc_edit(db, source.display_name, "rag_docs", user)
    manager = _get_kb_manager()
    try:
        result = await asyncio.to_thread(
            ingest_document_version,
            db,
            manager,
            source,
            "rag_docs",
            user,
            duplicate_action,
            versions_root,
        )
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
        logger.exception("upload failed", extra={"user_id": user["id"], "upload_filename": filename})
        raise HTTPException(status_code=400, detail="文件处理失败") from exc

    return {
        "status": result.status,
        "filename": result.filename,
        "chunks": result.chunks,
        "document_id": result.document_id,
        "version_id": result.version_id,
        "version_no": result.version_no,
    }


def _refresh_default_pipeline():
    try:
        with get_pipeline_lock().write():
            try:
                set_pipeline(_make_pipeline(kb_id="rag_docs"))
            except Exception:
                logger.exception("Pipeline refresh failed")
    except Exception:
        pass


@router.get("/files", summary="列出可索引文件", description="列出 data/upload/ 目录下的所有支持格式文件")
async def list_files(
    user_dict: dict | None = Depends(optional_user),
):
    db = get_user_db()
    data_dir = _get_upload_dir()

    files = []
    if data_dir.is_dir():
        for fpath in sorted(data_dir.iterdir()):
            if fpath.is_file() and fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                size = fpath.stat().st_size
                files.append(
                    {
                        "name": fpath.name,
                        "size": size,
                        "size_human": _human_size(size),
                        "ext": fpath.suffix.lower(),
                    }
                )

    if user_dict:
        existing_names = {item["name"] for item in files}
        visible_names = await asyncio.to_thread(
            db.get_accessible_doc_names, "rag_docs", user_dict["id"]
        )
        for name in visible_names:
            if name in existing_names:
                continue
            perm = await asyncio.to_thread(db.get_document_permission, name, "rag_docs")
            if perm is None:
                continue
            versions = await asyncio.to_thread(db.list_document_versions, perm["id"])
            version = next((item for item in versions if item["status"] in {"current", "staging"}), None)
            if version is None:
                continue
            source = Path(version["file_path"])
            size = source.stat().st_size if source.is_file() else 0
            files.append({
                "name": name,
                "size": size,
                "size_human": _human_size(size),
                "ext": Path(name).suffix.lower(),
                "document_id": perm["id"],
                "version_id": version["id"],
                "is_public": perm.get("scope") == "public",
                "protected": perm.get("protected", False),
                "owner_id": perm.get("owner_id"),
                "is_owner": perm.get("owner_id") == user_dict["id"],
            })

    if files:
        filenames = [f["name"] for f in files]
        perm_map = await asyncio.to_thread(
            db.get_document_permissions_by_names, filenames, "rag_docs"
        )
        accessible_names = set()
        if user_dict:
            accessible_names = set(await asyncio.to_thread(
                db.get_accessible_doc_names, "rag_docs", user_dict["id"]
            ))
        repo_files = _get_repo_file_names(data_dir)
        filtered_files = []
        for f in files:
            perm = perm_map.get(f["name"])
            if perm:
                f["document_id"] = perm["id"]
                scope = perm.get("scope", "private")
                f["is_public"] = scope == "public"
                f["protected"] = perm.get("protected", False)
                f["owner_id"] = perm.get("owner_id")
                f["is_owner"] = bool(user_dict and perm["owner_id"] == user_dict["id"])
                if user_dict and f["name"] not in accessible_names:
                    continue
                if not user_dict and scope != "public":
                    continue
            else:
                if f["name"] in repo_files:
                    f["is_public"] = True
                    f["protected"] = True
                    f["owner_id"] = 0
                    f["is_owner"] = False
                else:
                    continue
            filtered_files.append(f)
        files = filtered_files

    if files:
        filenames = [f["name"] for f in files]
        in_kb_set = await asyncio.to_thread(_check_files_in_kb, filenames, db)
        for f in files:
            f["in_kb"] = f["name"] in in_kb_set
    return {"files": files, "count": len(files)}


@router.post(
    "/upload",
    summary="上传文件",
    description="上传文件到 data/upload/ 并索引，支持 txt/md/pdf/docx/xlsx/csv 格式，最大 10MB",
)
async def upload_file(
    file: UploadFile = File(..., description="要上传的文件"),
    duplicate_action: str = Form("reject"),
    user: dict = Depends(require_user),
):
    db = get_user_db()
    content = await file.read(MAX_FILE_SIZE + 1)
    result = await _ingest_default_document(
        content=content,
        filename=file.filename or "upload.txt",
        user=user,
        db=db,
        duplicate_action=duplicate_action,
    )
    _refresh_default_pipeline()
    return result


@router.delete("/files/{filename}", summary="删除文件", description="从 data/upload/ 删除文件并重新索引")
async def delete_file(
    filename: str,
    request: Request,
    user_dict: dict = Depends(require_user),
):
    db = get_user_db(request)

    data_dir = _get_upload_dir()
    versions_root = _get_versions_root()
    safe_name = Path(filename).name
    legacy_path = data_dir / safe_name
    perm = db.get_document_permission(safe_name, "rag_docs")
    version_paths: list[Path] = []
    if perm:
        version_paths = [
            resolve_version_path(versions_root, version["file_path"])
            for version in db.list_document_versions(perm["id"])
        ]

    if not legacy_path.is_file() and not any(path.is_file() for path in version_paths):
        raise HTTPException(status_code=404, detail=f"文件 {safe_name} 不存在")

    if perm and perm.get("protected"):
        raise HTTPException(status_code=403, detail=f"文件 {safe_name} 受保护，不可删除")

    await asyncio.to_thread(perm_mod.check_doc_permission, db, safe_name, "rag_docs", user_dict, "delete")

    try:
        delete_doc(COLLECTION_NAME, safe_name)
        logger.info("向量已删除: %s", safe_name)
    except Exception as exc:
        logger.exception("删除向量失败: %s", safe_name)
        # 不把底层异常文本回显给客户端（B-05）。
        raise HTTPException(status_code=500, detail="删除向量失败") from exc

    legacy_path.unlink(missing_ok=True)
    for version_path in version_paths:
        version_path.unlink(missing_ok=True)

    if perm:
        try:
            db.delete_document_permission(perm["id"])
        except Exception as e:
            logger.warning("清理权限记录失败: %s", e)

    _refresh_default_pipeline()

    return {"status": "deleted", "filename": safe_name}


@router.put("/files/{filename}/visibility", summary="切换文件公开/私有")
async def toggle_file_visibility(
    filename: str,
    user_dict: dict = Depends(require_user),
):
    safe_name = Path(filename).name
    db = get_user_db()

    def _toggle():
        perm = db.get_document_permission(safe_name, "rag_docs")
        if not perm:
            raise HTTPException(status_code=404, detail="文件无权限记录")
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文件所有者可切换")
        new_val = db.toggle_document_visibility(perm["id"])
        return {"filename": safe_name, "is_public": new_val}

    return await asyncio.to_thread(_toggle)


@router.get("/files/{filename}/download", summary="下载文件")
async def download_file(
    filename: str,
    version_id: int | None = Query(default=None, ge=1),
    user_dict: dict = Depends(require_user),
):
    db = get_user_db()
    data_dir = _get_upload_dir()
    versions_root = _get_versions_root()
    safe_name = Path(filename).name
    perm = db.get_document_permission(safe_name, "rag_docs")
    file_path = data_dir / safe_name
    if perm:
        current_version = db.get_current_document_version(perm["id"])
        transferable_version = current_version
        if version_id is not None:
            candidate = db.get_document_version_by_id(version_id)
            if (
                candidate is None
                or candidate["document_id"] != perm["id"]
                or candidate["status"] not in {"current", "staging"}
            ):
                raise HTTPException(status_code=404, detail="文件版本不存在")
            transferable_version = candidate
        elif transferable_version is None:
            transferable_version = next(
                (
                    version
                    for version in db.list_document_versions(perm["id"])
                    if version["status"] == "staging"
                ),
                None,
            )
        if transferable_version:
            file_path = resolve_version_path(
                versions_root, transferable_version["file_path"]
            )

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件 {safe_name} 不存在")

    if not perm:
        if not user_dict.get("is_admin"):
            raise HTTPException(status_code=403, detail="无权下载该文件")
    elif not user_dict.get("is_admin"):
        scope = perm.get("scope", "private")
        is_owner = perm["owner_id"] == user_dict["id"]

        if scope == "private" and not is_owner:
            raise HTTPException(status_code=403, detail="无权下载该文件")
        if scope == "shared" and not is_owner and not db.is_document_shared(perm["id"], user_dict["id"]):
            raise HTTPException(status_code=403, detail="无权下载该文件")

        if not perm.get("downloadable", True) and not is_owner:
            raise HTTPException(status_code=403, detail="该文件不允许下载")

    return FileResponse(
        path=str(file_path),
        filename=safe_name,
        media_type="application/octet-stream",
    )


@router.post("/files/{filename}/tags", summary="为文档添加标签", description="为指定文档的所有分块添加标签")
async def add_tags_to_file(
    filename: str,
    tags: list[str],
    user_dict: dict = Depends(require_user),
):
    db = get_user_db()
    safe_name = Path(filename).name
    await asyncio.to_thread(perm_mod.check_doc_permission, db, safe_name, "rag_docs", user_dict, "edit")

    def _bulk_update_tags():
        client = vector_store._get_client()
        if not client.collection_exists(COLLECTION_NAME):
            raise HTTPException(status_code=404, detail="集合不存在")

        search_filter = Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=safe_name))])
        points = []
        offset = None
        while True:
            batch, offset = client.scroll(
                collection_name=COLLECTION_NAME,
                limit=10000,
                offset=offset,
                scroll_filter=search_filter,
                with_payload=False,
            )
            points.extend(batch)
            if offset is None:
                break
        if not points:
            raise HTTPException(status_code=404, detail=f"文档 {safe_name} 不存在")

        point_ids = [point.id for point in points]
        existing_points = client.retrieve(
            collection_name=COLLECTION_NAME,
            ids=point_ids,
            with_payload=True,
        )
        merged_tags = set(tags)
        for point in existing_points:
            if point.payload:
                merged_tags.update(point.payload.get("tags") or [])
        merged = sorted(merged_tags)
        client.set_payload(
            collection_name=COLLECTION_NAME,
            payload={"tags": merged},
            points=point_ids,
        )
        return point_ids

    point_ids = await asyncio.to_thread(_bulk_update_tags)
    return {"status": "updated", "filename": safe_name, "tags": tags, "points_updated": len(point_ids)}


@router.get("/tags", summary="获取所有标签", description="获取当前知识库中所有已使用的标签")
async def list_tags(user: dict = Depends(require_user)):
    db = get_user_db()
    client = vector_store._get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return {"tags": []}

    if user.get("is_admin"):
        permissions = await asyncio.to_thread(db.list_document_permissions_all)
        visible_names = {
            item["doc_name"] for item in permissions if item["kb_id"] == "rag_docs"
        }
    else:
        visible_names = set(await asyncio.to_thread(
            db.get_accessible_doc_names, "rag_docs", int(user["id"])
        ))

    all_tags = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=10000,
            offset=offset,
            with_payload=True,
        )
        for p in points:
            if (
                p.payload
                and p.payload.get("doc_name") in visible_names
                and "tags" in p.payload
            ):
                all_tags.update(p.payload["tags"])
        if offset is None:
            break

    return {"tags": sorted(all_tags)}


@router.post("/index-all", summary="索引全部文件", description="索引 data/upload/ 目录下的所有文件")
async def index_all(user: dict = Depends(require_admin)):
    data_dir = _get_upload_dir()
    if not data_dir.is_dir():
        raise HTTPException(status_code=400, detail="data/upload/ 目录不存在")
    try:
        stats = await asyncio.to_thread(index_folder, str(data_dir))
    except Exception as exc:
        logger.exception("full reindex failed", extra={"user_id": user["id"]})
        raise HTTPException(status_code=400, detail="全量索引失败") from exc

    _refresh_default_pipeline()
    return {"status": "indexed", **stats}


@router.post("/index", summary="索引文档", description="上传文档并添加到知识库，支持 txt/md/pdf/docx/xlsx 格式，最大 10MB")
async def index(
    file: UploadFile = File(..., description="要索引的文档文件"),
    duplicate_action: str = Form("reject"),
    user: dict = Depends(require_user),
):
    db = get_user_db()
    content = await file.read(MAX_FILE_SIZE + 1)
    result = await _ingest_default_document(
        content=content,
        filename=file.filename or "upload.txt",
        user=user,
        db=db,
        duplicate_action=duplicate_action,
    )
    _refresh_default_pipeline()
    return {**result, "status": "indexed"}


@router.post("/batch-import", summary="批量导入", description="上传 Excel/CSV 批量导入到知识库")
async def batch_import(
    file: UploadFile = File(...),
    mode: str = "qa_pair",
    config: str = "{}",
    user: dict = Depends(require_user),
):
    db = get_user_db()
    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件过大")

    suffix = Path(file.filename).suffix.lower() if file.filename else ".csv"
    if suffix not in {".csv", ".xlsx"}:
        raise HTTPException(status_code=400, detail=f"不支持的格式: {suffix}")

    doc_name = Path(file.filename or "upload").stem
    if await asyncio.to_thread(db.get_document_permission, doc_name, "rag_docs"):
        raise HTTPException(status_code=409, detail="同名文件已存在")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        parsed_config = _json.loads(config)
    except _json.JSONDecodeError:
        parsed_config = {}

    try:
        importer = BatchImporter()
        res = await asyncio.to_thread(
            importer.import_file,
            tmp_path,
            mode=mode,
            kb_id="rag_docs",
            user=user,
            user_db=db,
            config=parsed_config,
            doc_name=doc_name,
        )

        _refresh_default_pipeline()

        return {"status": "imported", "chunks": res["chunks"], "mode": mode}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="批量导入失败") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("batch import failed", extra={"user_id": user["id"], "doc_name": doc_name})
        raise HTTPException(status_code=400, detail="批量导入失败") from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.get("/documents/{doc_id}/permissions", summary="查看文档权限信息")
async def get_document_permissions(
    doc_id: int,
    user_dict: dict = Depends(require_user),
):
    db = get_user_db()

    def _get():
        perm = db.get_document_permission_by_id(doc_id)
        if not perm:
            return None
        require_personal_document_acl(db, perm["kb_id"])
        if (
            not user_dict.get("is_admin")
            and perm["owner_id"] != user_dict["id"]
            and not (
                perm.get("scope") == "shared"
                and db.is_document_shared(doc_id, user_dict["id"])
            )
            and perm.get("scope") != "public"
        ):
            raise HTTPException(status_code=403, detail="无权查看该文档权限信息")
        shared_users = db.list_shared_users(doc_id)
        owner = db.get_user_by_id(perm["owner_id"])
        return {
            "doc_id": doc_id,
            "doc_name": perm["doc_name"],
            "is_public": perm.get("scope") == "public",
            "protected": perm.get("protected", False),
            "owner": {"id": owner["id"], "username": owner["username"]} if owner else None,
            "shared_with": shared_users,
        }

    result = await asyncio.to_thread(_get)
    if not result:
        raise HTTPException(status_code=404, detail="文档权限记录不存在")
    return result


@router.put("/documents/{doc_id}/permission", summary="切换文档公开/私有")
async def update_document_permission(
    doc_id: int,
    user_dict: dict = Depends(require_user),
):
    db = get_user_db()

    def _toggle():
        perm = db.get_document_permission_by_id(doc_id)
        if not perm:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_personal_document_acl(db, perm["kb_id"])
        require_doc_edit(db, perm["doc_name"], perm["kb_id"], user_dict)
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文档上传者或管理员可修改权限")
        new_val = db.toggle_document_visibility(doc_id)
        return {"id": doc_id, "is_public": new_val}

    return await asyncio.to_thread(_toggle)


@router.post("/documents/{doc_id}/share", summary="共享文档给指定用户")
async def share_document(
    doc_id: int,
    req: ShareRequest,
    user_dict: dict = Depends(require_user),
):
    db = get_user_db()

    def _share():
        perm = db.get_document_permission_by_id(doc_id)
        if not perm:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_personal_document_acl(db, perm["kb_id"])
        require_doc_edit(db, perm["doc_name"], perm["kb_id"], user_dict)
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文档上传者或管理员可共享")
        target = db.get_user_by_id(req.user_id)
        if not target:
            raise HTTPException(status_code=404, detail=f"用户 ID {req.user_id} 不存在")
        try:
            db.share_document(doc_id, req.user_id, user_dict["id"], req.permission)
        except _sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="该用户已被共享") from exc
        return {
            "doc_id": doc_id,
            "user_id": req.user_id,
            "granted_by": user_dict["id"],
            "permission": req.permission,
        }

    return await asyncio.to_thread(_share)


@router.delete("/documents/{doc_id}/share/{user_id}", summary="撤销文档共享", status_code=204)
async def unshare_document(
    doc_id: int,
    user_id: int,
    user_dict: dict = Depends(require_user),
):
    db = get_user_db()

    def _unshare():
        perm = db.get_document_permission_by_id(doc_id)
        if not perm:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_personal_document_acl(db, perm["kb_id"])
        require_doc_edit(db, perm["doc_name"], perm["kb_id"], user_dict)
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文档上传者或管理员可撤销共享")
        db.unshare_document(doc_id, user_id)

    await asyncio.to_thread(_unshare)
    return None
