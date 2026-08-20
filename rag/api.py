import asyncio
import contextvars
import hashlib
import logging
import os
import re
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Security, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import settings

from rag.auth import create_token, decode_token, verify_api_key
from rag.backups.scheduler import start_backup_scheduler, stop_backup_scheduler
from rag.api_security import (
    log_security_mode,
    redact_source_config,
    require_admin,
    require_data_sources_admin,
    require_doc_edit,
    require_personal_document_acl,
    require_kb_edit,
    require_kb_view,
    require_user,
)
from rag.document_versions import (
    DuplicateDocument,
    choose_document_name,
    ingest_document_version,
)
from rag.document_sources.upload import UploadDocumentSource
from rag.kb_metadata import generate_summary, generate_toc
from rag.kb_file_store import resolve_stored_path
from rag.loader import load as load_document
from rag.knowledge_base import KnowledgeBaseManager
from rag.logging_config import set_request_id, setup_logging
from rag.materials.api import router as materials_router
from rag.spreadsheets.api import router as spreadsheets_router
from rag.operations_api import router as operations_router
from rag.org_auth.api import router as organization_router
from rag.permissions import filter_chunks_by_permission
from rag.reliability.api import answers_router, router as reliability_router
from rag.user_db import UserDB

# Initialize logging at module level so it is active before any request arrives.
setup_logging()

logger = logging.getLogger(__name__)

from rag.concurrency import ReadWriteLock


@asynccontextmanager
async def lifespan(_app: FastAPI):
    auto_index_on_startup()
    start_backup_scheduler()
    try:
        yield
    finally:
        stop_backup_scheduler()
        close_pipeline_processors()


app = FastAPI(
    title="RAG 知识库 API",
    description="基于检索增强生成的智能知识库系统",
    lifespan=lifespan,
)
pipeline = None
_pipelines = {}
_pipeline_lock = ReadWriteLock()
_material_worker_stop = threading.Event()
_material_worker_thread: threading.Thread | None = None
_material_worker = None

# Versioned document ingest (trusted knowledge loop).
app.include_router(operations_router)
app.include_router(materials_router)
app.include_router(spreadsheets_router)
app.include_router(reliability_router)
app.include_router(answers_router)
app.include_router(organization_router)


def _versions_root() -> Path:
    """Resolve the configured document-versions root from app state."""
    return Path(getattr(app.state, "document_versions_root", settings.document_versions_root))


def _run_document_version_migration() -> None:
    """Create version-1 rows for documents created before versioning."""
    from rag.document_versions import migrate_existing_document_versions

    try:
        report = migrate_existing_document_versions(
            user_db, _versions_root(), DATA_DIR
        )
        if report.migrated or report.missing:
            logger.info(
                "document version migration: migrated=%s missing=%s unchanged=%s",
                report.migrated,
                report.missing,
                report.unchanged,
            )
    except Exception:
        logger.exception("document version migration failed")


def _run_material_metadata_migration() -> None:
    """Backfill material FTS/metadata rows without re-embedding content."""
    from rag.materials.migration import migrate_material_metadata

    try:
        user_db.create_material_schema()
        report = migrate_material_metadata(user_db)
        if report.migrated_documents:
            logger.info(
                "material metadata migration: migrated=%s missing=%s",
                report.migrated_documents,
                report.missing_sources,
            )
    except Exception:
        logger.exception("material metadata migration failed")


def _run_spreadsheet_migration() -> None:
    from rag.spreadsheets.migration import migrate_existing_spreadsheets
    try:
        report = migrate_existing_spreadsheets(
            user_db,
            Path(getattr(app.state, "structured_datasets_root", settings.structured_datasets_root)),
        )
        if report.migrated or report.failed:
            logger.info("spreadsheet migration: migrated=%s failed=%s", report.migrated, report.failed)
    except Exception:
        logger.exception("spreadsheet migration failed")


def _start_material_worker() -> None:
    """Start the single API-owned consumer for persistent material jobs."""
    global _material_worker, _material_worker_thread
    if not settings.material_worker_enabled:
        return
    if _material_worker_thread is not None and _material_worker_thread.is_alive():
        return

    from rag.embedder import embed
    from rag.materials.formats import build_default_format_registry
    from rag.materials.worker import MaterialWorker, WorkerServices

    worker = MaterialWorker(
        WorkerServices(
            db=user_db,
            versions_root=_versions_root(),
            registry=build_default_format_registry(),
            embedder=embed,
            max_embedding_concurrency=settings.material_embedding_slots,
        ),
        worker_id=f"api-material-{uuid.uuid4().hex[:8]}",
        lease_seconds=settings.material_worker_lease_seconds,
        max_attempts=settings.material_job_max_attempts,
    )
    _material_worker_stop.clear()

    def _consume() -> None:
        while not _material_worker_stop.is_set():
            try:
                worker.run(batch=settings.material_worker_count)
            except Exception:
                logger.exception("material index worker polling iteration failed")
            _material_worker_stop.wait(1.0)

    _material_worker = worker
    _material_worker_thread = threading.Thread(
        target=_consume, name="material-index-worker", daemon=True
    )
    _material_worker_thread.start()
    logger.info("material index worker started")


def _stop_material_worker() -> bool:
    """Stop the API-owned worker before closing shared vector resources."""
    global _material_worker, _material_worker_thread
    worker = _material_worker
    thread = _material_worker_thread
    _material_worker_stop.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=5.0)
    if thread is not None and thread.is_alive():
        logger.error("material index worker did not stop before shutdown; preserving shared resources")
        return False
    if worker is not None:
        worker.shutdown()
    _material_worker = None
    _material_worker_thread = None
    return True


def auto_index_on_startup():
    """启动时增量索引 data/upload/ 下的文件。"""
    global pipeline
    log_security_mode()
    _run_document_version_migration()
    _run_material_metadata_migration()
    _run_spreadsheet_migration()
    if not DATA_DIR.is_dir():
        _start_material_worker()
        return

    from rag.folder_indexer import compute_file_hash, load_index_state, save_index_state, diff_index, index_folder
    state_path = str(DATA_DIR.parent / "index_state.json")
    stored_state = load_index_state(state_path)

    # 计算当前文件 hash
    current_hashes = {}
    for fpath in DATA_DIR.iterdir():
        if fpath.is_file() and fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
            current_hashes[fpath.name] = compute_file_hash(str(fpath))

    added, modified, deleted = diff_index(current_hashes, stored_state)
    changed = added + modified

    # The file manifest alone cannot prove that the vector index still exists.
    # Treat a missing Qdrant collection as stale so a restored/new data volume
    # is rebuilt instead of constructing a Retriever against a nonexistent one.
    collection_check_failed = False
    try:
        from rag.vector_store import COLLECTION_NAME, _get_client

        collection_available = _get_client().collection_exists(COLLECTION_NAME)
    except Exception as e:
        logger.warning("无法确认向量索引状态，将跳过本次自动索引: %s", e)
        collection_check_failed = True
        collection_available = None
    if collection_available is False:
        logger.warning("向量索引集合不存在，将重建索引")
        changed = list(current_hashes)

    # 管理员初始化：首次启动时根据环境变量设置管理员
    def _init_admin():
        admin_username = os.getenv("INIT_ADMIN_USERNAME")
        if not admin_username:
            return
        target = user_db.get_user_by_username(admin_username)
        if target and not target.get("is_admin"):
            user_db.set_user_admin(target["id"], True)
            logger.info("已将用户 %s 设置为管理员", admin_username)

    # 同步仓库文件到服务器（从 /app/repo_upload/ 复制，不被 volume 覆盖）
    # owner_id=0 表示系统所有（不依赖用户是否存在）
    SYSTEM_OWNER_ID = 0

    def _sync_repo_files():
        import shutil
        # Docker 构建时把仓库文件备份到 /app/repo_upload/
        repo_upload = Path("/app/repo_upload")
        if not repo_upload.is_dir():
            # 非 Docker 环境，用 git 仓库路径
            repo_upload = Path(__file__).resolve().parent.parent / "data" / "upload"
        if not repo_upload.is_dir():
            return

        # 读取 .repo_files manifest，只处理仓库文件（不含用户上传）
        manifest_path = repo_upload / ".repo_files"
        repo_file_names = set()
        if manifest_path.is_file():
            for line in manifest_path.read_text(encoding="utf-8").splitlines():
                name = line.strip()
                if name:
                    repo_file_names.add(name)
        else:
            # 无 manifest 时回退：处理 repo_upload 下所有文件
            for fpath in repo_upload.iterdir():
                if fpath.is_file() and fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                    repo_file_names.add(fpath.name)

        if not DATA_DIR.is_dir():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        for fname in repo_file_names:
            src = repo_upload / fname
            if not src.is_file():
                continue
            dest = DATA_DIR / fname
            if not dest.exists():
                shutil.copy2(str(src), str(dest))
                logger.info("同步仓库文件: %s", fname)
            existing = user_db.get_document_permission(fname, "rag_docs")
            if not existing:
                user_db.create_document_permission(
                    fname, "rag_docs", SYSTEM_OWNER_ID,
                    is_public=True, protected=True
                )
                logger.info("创建受保护权限: %s (owner=system)", fname)
            elif existing.get("owner_id") == SYSTEM_OWNER_ID and (not existing.get("protected") or existing.get("scope") != "public"):
                # 只修复系统所有者(仓库文件)的记录，不修改用户上传的文件
                with user_db._lock:
                    user_db._conn.execute(
                        "UPDATE document_permissions SET protected = 1, is_public = 1, scope = 'public' WHERE id = ?",
                        (existing["id"],)
                    )
                    user_db._conn.commit()
                logger.info("更新为受保护+公开: %s", fname)

    if collection_check_failed:
        try:
            _init_admin()
            _sync_repo_files()
        except Exception as e:
            logger.warning("管理员初始化失败: %s", e)
        return

    if not changed and not deleted:
        logger.info("索引无需更新（%d 文件）", len(current_hashes))
        with _pipeline_lock.write():
            pipeline = _get_or_create_pipeline("rag_docs")
        _init_admin()
        _sync_repo_files()
        _start_material_worker()
        return

    logger.info("索引变化: %d 新增, %d 修改, %d 删除", len(added), len(modified), len(deleted))

    try:
        # 如果有变化，重新索引全部（简单可靠）
        if changed:
            stats = index_folder(str(DATA_DIR))
            logger.info("索引完成: %d 文件, %d 分块", stats["files"], stats["chunks"])

        # 更新状态
        new_state = {"files": {}}
        for name, h in current_hashes.items():
            new_state["files"][name] = {"hash": h}
        save_index_state(state_path, new_state)

        with _pipeline_lock.write():
            pipeline = _get_or_create_pipeline("rag_docs")
    except Exception as e:
        logger.warning("启动索引失败: %s", e)

    try:
        _init_admin()
        _sync_repo_files()
    except Exception as e:
        logger.warning("管理员初始化失败: %s", e)
    _start_material_worker()


@app.middleware("http")
async def request_id_middleware(request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    set_request_id(request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# Vue Router SPA fallback: browser requests to known frontend paths → serve index.html
_VUE_ROUTES = {"/files", "/kb", "/analysis", "/analytics", "/operations", "/settings/password", "/mode/file", "/mode/kb", "/mode/analysis"}
_VUE_ROUTE_PREFIXES = {"/kb/"}
_VUE_ROUTE_PATTERNS = (
    re.compile(r"^/documents/\d+/versions/\d+/spreadsheet$"),
)


@app.middleware("http")
async def spa_fallback_middleware(request, call_next):
    path = request.url.path
    accept = request.headers.get("accept", "")
    # API 请求（带 Authorization header）不走 SPA fallback
    has_auth = "authorization" in {k.lower() for k in request.headers}
    # Only for GET requests from browsers to known Vue routes (no auth header)
    if request.method == "GET" and "text/html" in accept and not has_auth:
        is_frontend_route = (
            path in _VUE_ROUTES
            or any(path.startswith(p) for p in _VUE_ROUTE_PREFIXES)
            or any(pattern.fullmatch(path) for pattern in _VUE_ROUTE_PATTERNS)
        )
        if is_frontend_route:
            index = STATIC_DIR / "index.html"
            if index.exists():
                resp = FileResponse(index, media_type="text/html")
                resp.headers["Vary"] = "Authorization"
                resp.headers["Cache-Control"] = "no-store"
                return resp
    response = await call_next(request)
    response.headers["Vary"] = "Authorization"
    return response


# 数据目录
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "upload"
KB_FILES_ROOT = Path(__file__).resolve().parent.parent / "data" / "kb_files"
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv"}

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _get_repo_file_names() -> set[str]:
    """读取 .repo_files manifest，返回仓库文件名集合。"""
    manifest = DATA_DIR / ".repo_files"
    if not manifest.is_file():
        return set()
    names = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name:
            names.add(name)
    return names


class QueryRequest(BaseModel):
    question: str = Field(..., description="用户提问")
    top_k: int = Field(default=5, ge=1, description="返回相关文档数量")
    session_id: str | None = Field(default=None, description="会话 ID（用于多轮对话记忆）")
    conversation_id: int | None = Field(default=None, description="对话 ID")
    doc_name: str | None = Field(default=None, description="限定检索的文档名称")
    tags: list[str] | None = Field(default=None, description="按标签过滤文档")


class QueryResponse(BaseModel):
    answer: str = Field(..., description="回答内容")
    sources: list[dict] = Field(default=[], description="引用来源")


class StreamQueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=5, ge=1)
    session_id: str | None = None
    conversation_id: int | None = None
    doc_name: str | None = None
    tags: list[str] | None = None
    kb_id: str | None = None


class SuggestRequest(BaseModel):
    question: str = Field(..., max_length=2000)
    answer: str = Field(..., max_length=5000)


class FeedbackRequest(BaseModel):
    message_id: int
    value: str = Field(pattern="^(positive|negative)$")
    comment: str = None


class RegenerateRequest(BaseModel):
    conversation_id: int
    message_id: int


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=8, description="密码至少8位，需含大写字母、小写字母和数字")


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    username: str


class CreateConversationRequest(BaseModel):
    mode: str = Field(default="file", pattern="^(file|kb|analysis)$", description="对话模式")


class CreateCardRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="卡片名称")


class RenameCardRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="新名称")


class AddQuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=5000, description="问题内容")
    answer: str = Field(default="", description="回答内容")
    source_mode: str = Field(default="", description="来源模式")
    source_message_id: int | None = Field(default=None, description="来源消息 ID")


class UpdateSummaryRequest(BaseModel):
    summary: str = Field(..., description="摘要内容")


class SuggestCardRequest(BaseModel):
    question: str = Field(..., max_length=5000, description="问题内容")
    answer: str = Field(..., max_length=10000, description="回答内容")


class UpdateRoleRequest(BaseModel):
    is_admin: bool | None = None


# Module-level UserDB instance
from config import settings as _settings
_DB_PATH = Path(_settings.users_db_path)
user_db = UserDB(str(_DB_PATH))
app.state.user_db = user_db
app.state.document_versions_root = _settings.document_versions_root
app.state.structured_datasets_root = _settings.structured_datasets_root
app.state.kb_files_root = str(KB_FILES_ROOT)


# ── Pipeline 数据处理器单例 ─────────────────────────────────────────────
_feedback_processor = None
_gap_analyzer = None
_processor_lock = threading.Lock()


def get_feedback_processor():
    """返回模块级 FeedbackProcessor 单例，避免每次查询都创建新实例。"""
    global _feedback_processor
    with _processor_lock:
        if _feedback_processor is None:
            from rag.feedback_processor import FeedbackProcessor

            _feedback_processor = FeedbackProcessor(_settings.memory_db_path)
    return _feedback_processor


def get_gap_analyzer():
    """返回模块级 GapAnalyzer 单例，避免每次查询都创建新实例。"""
    global _gap_analyzer
    with _processor_lock:
        if _gap_analyzer is None:
            from rag.gap_analyzer import GapAnalyzer

            _gap_analyzer = GapAnalyzer(_settings.memory_db_path)
    return _gap_analyzer


def _make_pipeline(**kwargs):
    """构造使用 API 共享数据库连接的 pipeline。"""
    from rag.pipeline import RAGPipeline

    kwargs.setdefault("user_db", user_db)
    if "feedback_processor" not in kwargs:
        kwargs["feedback_processor"] = get_feedback_processor()
    if "gap_analyzer" not in kwargs:
        kwargs["gap_analyzer"] = get_gap_analyzer()
    return RAGPipeline(**kwargs)


def _get_or_create_pipeline(kb_id: str):
    """Return the cached pipeline for a knowledge base (caller holds write lock)."""
    global pipeline
    cached = _pipelines.get(kb_id)
    if cached is None and kb_id == "rag_docs" and pipeline is not None:
        cached = pipeline
        _pipelines[kb_id] = cached
    if cached is None:
        cached = _make_pipeline(kb_id=kb_id)
        _pipelines[kb_id] = cached
    if kb_id == "rag_docs":
        pipeline = cached
    return cached


class _ComparisonRetrieverMap:
    """Expose cached per-KB retrievers without broadening comparison access."""

    def get(self, kb_id: str):
        with _pipeline_lock.write():
            return _get_or_create_pipeline(kb_id).retriever


def _make_comparison_document_tool():
    from rag.reliability.document_tool import CurrentVersionDocumentEvidenceTool

    return CurrentVersionDocumentEvidenceTool(user_db, _ComparisonRetrieverMap())


app.state.comparison_document_tool_factory = _make_comparison_document_tool


def close_pipeline_processors():
    """关闭由 API 生命周期持有的共享处理器。"""
    global pipeline, _feedback_processor, _gap_analyzer
    if not _stop_material_worker():
        return
    with _pipeline_lock.write():
        cached_pipelines = list(dict.fromkeys(_pipelines.values()))
        _pipelines.clear()
        pipeline = None
    for cached_pipeline in cached_pipelines:
        close = getattr(cached_pipeline, "close", None)
        if callable(close):
            close()
    with _processor_lock:
        for processor in (_feedback_processor, _gap_analyzer):
            if processor is not None:
                processor.close()
        _feedback_processor = None
        _gap_analyzer = None
    from rag.vector_store import close_client
    close_client()


@app.get("/health", summary="健康检查", description="检查服务是否正常运行")
def health():
    components = {}
    try:
        from rag.vector_store import _get_client

        qdrant_client = _get_client()
        qdrant_client.get_collections()
        components["qdrant"] = "ok"
    except Exception as e:
        logger.error("Qdrant 健康检查失败: %s", e)
        components["qdrant"] = "error"

    try:
        import sqlite3

        from config import settings as _settings

        conn = sqlite3.connect(_settings.analysis_db_path)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        components["sqlite"] = "ok"
    except Exception as e:
        logger.error("SQLite 健康检查失败: %s", e)
        components["sqlite"] = "error"

    status = "healthy" if all(v == "ok" for v in components.values()) else "degraded"
    payload = {"status": status, "components": components}
    if status == "degraded":
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.post("/register", response_model=TokenResponse, summary="用户注册")
async def register(req: RegisterRequest):
    from rag.user_db import _validate_password_strength
    try:
        _validate_password_strength(req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        user_id = await asyncio.to_thread(user_db.create_user, req.username, req.password)
    except ValueError as e:
        if "已存在" in str(e):
            raise HTTPException(status_code=400, detail="用户名或密码错误") from e
        raise HTTPException(status_code=400, detail=str(e)) from e
    token = create_token({"user_id": user_id, "username": req.username})
    return TokenResponse(token=token, username=req.username)


@app.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(req: LoginRequest):
    user = await asyncio.to_thread(user_db.authenticate, req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    from rag.org_auth.repository import OrgRepository
    profile = await asyncio.to_thread(OrgRepository(user_db).get_profile_for_user, user["id"])
    if profile is not None and not profile.active:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_token({
        "user_id": user["id"], "username": user["username"],
        "security_version": profile.security_version if profile else None,
    })
    return TokenResponse(token=token, username=user["username"])


@app.get("/me", summary="获取当前用户信息")
async def get_me(user: dict = Depends(require_user)):
    from dataclasses import asdict
    from rag.org_auth.repository import OrgRepository
    profile = await asyncio.to_thread(OrgRepository(user_db).get_profile_for_user, user["id"])
    result = {"id": user["id"], "username": user["username"]}
    if profile is not None:
        result.update({"is_admin": bool(user.get("is_admin")), "profile": asdict(profile)})
    return result


async def _require_auth(authorization: str) -> dict:
    """要求认证，返回完整 user dict。无 token 或 token 无效时返回 401。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user_dict = await asyncio.to_thread(_get_current_user, token)
    if not user_dict:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return user_dict


async def _resolve_upload_user(authorization: str = Header(default="")) -> dict | None:
    """upload 端点鉴权依赖：认证开启时强制登录，认证关闭时允许匿名上传。

    Returns:
        认证开启：完整 user dict（缺失/无效 token 抛 401）
        认证关闭：None（匿名上传，兼容既有行为，不创建权限记录）
    """
    if not _settings.auth_enabled:
        return None
    return await _require_auth(authorization)


def _get_current_user(token: str) -> dict | None:
    """从 JWT token 获取当前用户。返回 None 表示认证失败。

    注意：此函数在 asyncio.to_thread 中调用，不能抛出 HTTPException
    （否则会被包装成 500 错误）。调用方必须检查返回值并抛出 HTTPException。
    """
    # 先解码 token（不检查 password_changed_at）
    payload = decode_token(token)
    if not payload:
        return None
    # 查出用户信息
    user = user_db.get_user_by_id(payload["user_id"])
    if not user:
        return None
    # 再用 password_changed_at 校验 token 是否因改密而失效
    password_changed_at = user.get("password_changed_at")
    if password_changed_at and payload.get("iat", 0) < password_changed_at:
        return None
    from rag.org_auth.repository import OrgRepository
    if not OrgRepository(user_db).token_is_current(user["id"], payload.get("security_version")):
        return None
    return user


def _check_kb_edit_permission(kb_id: str, user_dict: dict) -> None:
    """检查用户是否有 KB 编辑权限（owner / admin / 共享 edit）。

    Raises:
        HTTPException 403: 无编辑权限
        HTTPException 404: KB 不存在
    """
    from rag.org_auth.repository import OrgRepository
    if OrgRepository(user_db).get_kb_policy(kb_id) is not None:
        require_kb_edit(user_db, kb_id, user_dict)
        return
    meta = user_db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if user_dict.get("is_admin"):
        return
    if meta["owner_id"] == user_dict["id"]:
        return
    if user_db.is_kb_shared(kb_id, user_dict["id"], permission="edit"):
        return
    raise HTTPException(status_code=403, detail="仅知识库所有者、管理员或共享编辑者可操作")


@app.get("/users", summary="搜索用户", description="按用户名搜索用户，用于共享功能")
async def search_users(q: str = Query(..., min_length=2, description="搜索词，至少 2 个字符"), authorization: str = Header(default="")):
    await _require_auth(authorization)

    def _search():
        rows = user_db.search_users(q, limit=20)
        return [{"id": r["id"], "username": r["username"]} for r in rows]

    return await asyncio.to_thread(_search)


# ── Phase 1a: 用户管理端点 ──────────────────────────────────────────


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="旧密码")
    new_password: str = Field(..., description="新密码")
    confirm_password: str = Field(..., description="确认新密码")


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., description="新密码")


@app.put("/users/me/password", summary="修改密码")
async def change_password(req: ChangePasswordRequest, authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)

    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="新密码与确认密码不匹配")

    try:
        await asyncio.to_thread(user_db.change_password, user_dict["id"], req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"message": "密码已修改，请重新登录"}


@app.put("/users/{uid}/reset-password", summary="管理员重置密码")
async def reset_password(uid: int, req: ResetPasswordRequest, authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)
    if not user_dict.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可重置密码")

    try:
        await asyncio.to_thread(user_db.reset_password, uid, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"message": "密码已重置"}


@app.get("/conversations/search", summary="搜索对话")
async def search_conversations(q: str = Query(..., min_length=1, description="搜索词"), page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)

    def _search():
        results = user_db.search_conversations(user_dict["id"], q, page=page, size=size)
        return {"results": results, "page": page, "size": size}

    return await asyncio.to_thread(_search)


@app.post("/conversations", summary="新建对话")
async def create_conversation(req: CreateConversationRequest = CreateConversationRequest(), authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    cid = await asyncio.to_thread(user_db.create_conversation, user["id"], "", req.mode)
    return {"id": cid, "title": "新对话", "mode": req.mode}


@app.get("/conversations", summary="列出对话")
async def list_conversations(authorization: str = Header(default=""), mode: str | None = None):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    result = await asyncio.to_thread(user_db.list_conversations, user["id"], mode)
    logger.debug("list_conversations mode=%s user=%s count=%d modes=%s", mode, user["id"], len(result), [c.get("mode") for c in result])
    return result


@app.delete("/conversations/{cid}", summary="删除对话")
async def delete_conversation(cid: int, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    if not await asyncio.to_thread(user_db.delete_conversation, cid, user["id"]):
        raise HTTPException(status_code=404, detail="对话不存在")
    return {"status": "deleted"}


class GenerateTitleRequest(BaseModel):
    question: str = Field(..., description="用户的第一条问题")


@app.post("/conversations/{cid}/generate-title", summary="生成对话标题")
async def generate_conversation_title(cid: int, req: GenerateTitleRequest, authorization: str = Header(default="")):
    """用 LLM 生成对话标题，更新 conversations.title。"""
    user_dict = await _require_auth(authorization)

    def _generate():
        from rag.generator import generate
        prompt = f"请用一句话概括以下问题，作为对话标题，不超过20个字，不要加引号：\n{req.question}"
        try:
            title = generate([{"role": "user", "content": prompt}], temperature=0.3)
            title = title.strip().replace('"', '').replace("'", "").replace("。", "")
            if len(title) > 30:
                title = title[:30]
        except Exception:
            title = req.question[:20]

        # 更新数据库
        user_db.update_conversation_title(cid, user_dict["id"], title)
        return title

    title = await asyncio.to_thread(_generate)
    return {"title": title}


@app.get("/conversations/{cid}/messages", summary="获取对话消息")
async def get_conversation_messages(cid: int, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return await asyncio.to_thread(user_db.get_messages, cid, user["id"])


@app.post("/feedback", summary="提交反馈")
async def submit_feedback(req: FeedbackRequest, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    value_int = 1 if req.value == "positive" else -1
    owned = await asyncio.to_thread(user_db.message_belongs_to_user, req.message_id, user["id"])
    if not owned:
        raise HTTPException(status_code=404, detail="消息不存在或无权操作")
    await asyncio.to_thread(user_db.add_feedback, req.message_id, user["id"], value_int, req.comment or "")
    return {"status": "ok"}


@app.post("/regenerate", summary="重新生成回答")
async def regenerate(req: RegenerateRequest, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    messages = await asyncio.to_thread(user_db.get_messages, req.conversation_id, user["id"])
    target = None
    user_question = None
    for i, msg in enumerate(messages):
        if msg["id"] == req.message_id and msg["role"] == "assistant":
            target = msg
            if i > 0 and messages[i - 1]["role"] == "user":
                user_question = messages[i - 1]["content"]
            break

    if not target:
        raise HTTPException(status_code=404, detail="消息不存在")
    if not user_question:
        raise HTTPException(status_code=400, detail="找不到对应的用户问题")

    with _pipeline_lock.read():
        if pipeline is None:
            raise HTTPException(status_code=400, detail="尚未索引文档")
        current_pipeline = pipeline

    session_id = f"conv_{req.conversation_id}"
    prepared, error = current_pipeline._prepare_context(user_question, session_id, None, user=user, kb_id="rag_docs")
    if error:
        raise HTTPException(status_code=400, detail=error)

    if prepared["route"] == "fixed":
        new_answer = prepared["answer"]
    elif prepared["route"] == "agent":
        from rag.pipeline import _agent_run_kwargs

        # 权限上下文由 agent.run 的线程局部状态隔离，无需改写共享工具或持锁。
        new_answer = await asyncio.to_thread(
            current_pipeline.agent.run, user_question, **_agent_run_kwargs(prepared)
        )
    else:
        from rag.generator import generate
        new_answer = await asyncio.to_thread(generate, prepared["messages"], 0.7)

    await asyncio.to_thread(user_db.update_message, req.message_id, new_answer, user["id"])
    return {"message_id": req.message_id, "answer": new_answer}


@app.get("/files", summary="列出可索引文件", description="列出 data/upload/ 目录下的所有支持格式文件")
async def list_files(user_id: str = Security(verify_api_key), authorization: str = Header(default="")):
    # 获取当前用户
    user_dict = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
        user_dict = await asyncio.to_thread(_get_current_user, token)
    # 如果 JWT 没获取到，尝试用 user_id
    if not user_dict and user_id and user_id != "anonymous":
        user_dict = await asyncio.to_thread(user_db.get_user_by_id, int(user_id))

    files = []
    if DATA_DIR.is_dir():
        for fpath in sorted(DATA_DIR.iterdir()):
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

    # Material uploads live under immutable version paths rather than the
    # legacy upload directory; include visible staged/current documents too.
    if user_dict:
        existing_names = {item["name"] for item in files}
        visible_names = await asyncio.to_thread(
            user_db.get_accessible_doc_names, "rag_docs", user_dict["id"]
        )
        for name in visible_names:
            if name in existing_names:
                continue
            perm = await asyncio.to_thread(user_db.get_document_permission, name, "rag_docs")
            if perm is None:
                continue
            versions = await asyncio.to_thread(user_db.list_document_versions, perm["id"])
            version = next((item for item in versions if item["status"] in {"current", "staging"}), None)
            if version is None:
                continue
            source = Path(version["file_path"])
            size = source.stat().st_size if source.is_file() else 0
            files.append({"name": name, "size": size, "size_human": _human_size(size),
                          "ext": Path(name).suffix.lower(), "document_id": perm["id"],
                          "version_id": version["id"],
                          "is_public": perm.get("scope") == "public",
                          "protected": perm.get("protected", False),
                          "owner_id": perm.get("owner_id"),
                          "is_owner": perm.get("owner_id") == user_dict["id"]})

    # 批量查询权限信息并过滤
    if files:
        filenames = [f["name"] for f in files]
        perm_map = await asyncio.to_thread(
            user_db.get_document_permissions_by_names, filenames, "rag_docs"
        )
        accessible_names = set()
        if user_dict:
            accessible_names = set(await asyncio.to_thread(
                user_db.get_accessible_doc_names, "rag_docs", user_dict["id"]
            ))
        repo_files = _get_repo_file_names()  # 只读一次
        filtered_files = []
        for f in files:
            perm = perm_map.get(f["name"])
            if perm:
                f["document_id"] = perm["id"]
                scope = perm.get("scope", "private")
                f["is_public"] = scope == "public"
                f["protected"] = perm.get("protected", False)
                f["owner_id"] = perm.get("owner_id")
                f["is_owner"] = user_dict and perm["owner_id"] == user_dict["id"]
                if user_dict and f["name"] not in accessible_names:
                    continue
                if not user_dict and scope != "public":
                    continue
            else:
                # 无权限记录：检查是否为仓库文件
                if f["name"] in repo_files:
                    f["is_public"] = True
                    f["protected"] = True
                    f["owner_id"] = 0
                    f["is_owner"] = False
                else:
                    # A user file without a permission row cannot be proven public.
                    continue
            filtered_files.append(f)
        files = filtered_files

    # Batch check which files are in KBs
    if files:
        filenames = [f["name"] for f in files]
        in_kb_set = await asyncio.to_thread(_check_files_in_kb, filenames)
        for f in files:
            f["in_kb"] = f["name"] in in_kb_set
    return {"files": files, "count": len(files)}


def _check_files_in_kb(filenames: list[str]) -> set[str]:
    """批量检查哪些文件已编入知识库，返回已编入的文件名集合。"""
    import sqlite3
    db_path = str(_DB_PATH)
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        placeholders = ",".join("?" * len(filenames))
        rows = conn.execute(
            f"SELECT DISTINCT filename FROM kb_documents WHERE filename IN ({placeholders}) AND status = 'indexed'",
            filenames
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()
    finally:
        if conn:
            conn.close()


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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
    if existing_perm.get("scope") == "public":
        return True
    return False


async def _ingest_default_document(
    *, content: bytes, filename: str, user: dict, db: UserDB, duplicate_action: str = "reject"
) -> dict:
    """Ingest a private default-library document through the version service."""
    filename = Path(filename or "upload.txt").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的文件格式")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 10 MB 限制")
    if duplicate_action not in ("reject", "overwrite", "keep"):
        raise HTTPException(status_code=400, detail="无效的 duplicate_action")

    source = UploadDocumentSource.from_bytes(filename, content)
    if duplicate_action == "overwrite" and db.get_document_permission(source.display_name, "rag_docs"):
        require_doc_edit(db, source.display_name, "rag_docs", user)
    manager = KnowledgeBaseManager()
    try:
        result = await asyncio.to_thread(
            ingest_document_version,
            db,
            manager,
            source,
            "rag_docs",
            user,
            duplicate_action,
            _versions_root(),
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


@app.post(
    "/upload",
    summary="上传文件",
    description="上传文件到 data/upload/ 并索引，支持 txt/md/pdf/docx/xlsx/csv 格式，最大 10MB",
)
async def upload_file(
    file: UploadFile = File(..., description="要上传的文件"),
    duplicate_action: str = Form("reject"),
    user: dict = Depends(require_user),
):
    content = await file.read(MAX_FILE_SIZE + 1)
    result = await _ingest_default_document(
        content=content,
        filename=file.filename or "upload.txt",
        user=user,
        db=user_db,
        duplicate_action=duplicate_action,
    )

    global pipeline
    from rag.pipeline import RAGPipeline
    with _pipeline_lock.write():
        try:
            pipeline = RAGPipeline(kb_id="rag_docs", user_db=user_db)
        except Exception:
            logger.exception("Pipeline refresh failed after upload")
    return result


@app.delete("/files/{filename}", summary="删除文件", description="从 data/upload/ 删除文件并重新索引")
async def delete_file(filename: str, authorization: str = Header(default="")):
    from rag.vector_store import COLLECTION_NAME, delete_doc

    global pipeline

    # 权限校验（必须登录，先于文件存在性检查以避免信息泄露）
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    token = authorization.replace("Bearer ", "")
    user_dict = await asyncio.to_thread(_get_current_user, token)
    if not user_dict or user_dict.get("id") == "anonymous":
        raise HTTPException(status_code=401, detail="请先登录")

    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    legacy_path = DATA_DIR / safe_name
    perm = user_db.get_document_permission(safe_name, "rag_docs")
    version_paths: list[Path] = []
    if perm:
        from rag.document_version_store import resolve_version_path

        version_paths = [
            resolve_version_path(_versions_root(), version["file_path"])
            for version in user_db.list_document_versions(perm["id"])
        ]

    if not legacy_path.is_file() and not any(path.is_file() for path in version_paths):
        raise HTTPException(status_code=404, detail=f"文件 {safe_name} 不存在")

    # 检查是否为受保护文件
    if perm and perm.get("protected"):
        raise HTTPException(status_code=403, detail=f"文件 {safe_name} 受保护，不可删除")
    from rag.permissions import check_doc_permission
    await asyncio.to_thread(check_doc_permission, user_db, safe_name, "rag_docs", user_dict, "delete")

    # 先删向量，再删文件（确保向量删除成功）
    try:
        delete_doc(COLLECTION_NAME, safe_name)
        logger.info("向量已删除: %s", safe_name)
    except Exception as e:
        logger.error("删除向量失败: %s", e)
        raise HTTPException(status_code=500, detail=f"删除向量失败: {e}")

    # 向量删除成功后，再删文件
    legacy_path.unlink(missing_ok=True)
    for version_path in version_paths:
        version_path.unlink(missing_ok=True)

    # 清理权限记录（I19）
    if perm:
        try:
            user_db.delete_document_permission(perm["id"])
        except Exception as e:
            logger.warning("清理权限记录失败: %s", e)

    # 刷新 pipeline
    with _pipeline_lock.write():
        try:
            pipeline = _get_or_create_pipeline("rag_docs")
        except Exception as e:
            logger.error("Pipeline 刷新失败（删除后）: %s", e)

    return {"status": "deleted", "filename": safe_name}


@app.put("/files/{filename}/visibility", summary="切换文件公开/私有")
async def toggle_file_visibility(filename: str, authorization: str = Header(default="")):
    safe_name = Path(filename).name
    user_dict = await _require_auth(authorization)

    def _toggle():
        perm = user_db.get_document_permission(safe_name, "rag_docs")
        if not perm:
            raise HTTPException(status_code=404, detail="文件无权限记录")
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文件所有者可切换")
        new_val = user_db.toggle_document_visibility(perm["id"])
        return {"filename": safe_name, "is_public": new_val}

    try:
        return await asyncio.to_thread(_toggle)
    except HTTPException:
        raise


@app.get("/files/{filename}/download", summary="下载文件")
async def download_file(
    filename: str,
    version_id: int | None = Query(default=None, ge=1),
    authorization: str = Header(default=""),
):
    """下载文件。检查权限 + downloadable 字段。"""
    user_dict = await _require_auth(authorization)

    safe_name = Path(filename).name
    perm = user_db.get_document_permission(safe_name, "rag_docs")
    file_path = DATA_DIR / safe_name
    if perm:
        current_version = user_db.get_current_document_version(perm["id"])
        transferable_version = current_version
        if version_id is not None:
            candidate = user_db.get_document_version_by_id(version_id)
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
                    for version in user_db.list_document_versions(perm["id"])
                    if version["status"] == "staging"
                ),
                None,
            )
        if transferable_version:
            from rag.document_version_store import resolve_version_path

            file_path = resolve_version_path(
                _versions_root(), transferable_version["file_path"]
            )

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件 {safe_name} 不存在")

    # 权限检查
    if not perm:
        # 无权限记录 = 未归属/旧文档，默认拒绝下载（无法证明公开）；admin 除外
        if not user_dict.get("is_admin"):
            raise HTTPException(status_code=403, detail="无权下载该文件")
    elif not user_dict.get("is_admin"):
        # admin 可下载所有文件
        scope = perm.get("scope", "private")
        is_owner = perm["owner_id"] == user_dict["id"]

        # 检查可见权限
        if scope == "private" and not is_owner:
            raise HTTPException(status_code=403, detail="无权下载该文件")
        if scope == "shared" and not is_owner and not user_db.is_document_shared(perm["id"], user_dict["id"]):
            raise HTTPException(status_code=403, detail="无权下载该文件")

        # 检查 downloadable（owner 总是可下载）
        if not perm.get("downloadable", True) and not is_owner:
            raise HTTPException(status_code=403, detail="该文件不允许下载")

    return FileResponse(
        path=str(file_path),
        filename=safe_name,
        media_type="application/octet-stream",
    )


@app.post("/files/{filename}/tags", summary="为文档添加标签", description="为指定文档的所有分块添加标签")
async def add_tags_to_file(filename: str, tags: list[str], authorization: str = Header(default="")):
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    from rag.vector_store import _get_client, COLLECTION_NAME

    safe_name = Path(filename).name

    # 权限校验（必须登录）
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    token = authorization.replace("Bearer ", "")
    user_dict = await asyncio.to_thread(_get_current_user, token)
    if not user_dict or user_dict.get("id") == "anonymous":
        raise HTTPException(status_code=401, detail="请先登录")
    from rag.permissions import check_doc_permission
    await asyncio.to_thread(check_doc_permission, user_db, safe_name, "rag_docs", user_dict, "edit")

    def _bulk_update_tags():
        client = _get_client()
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


@app.get("/tags", summary="获取所有标签", description="获取当前知识库中所有已使用的标签")
async def list_tags(user_id: str = Security(verify_api_key)):
    from rag.vector_store import _get_client, COLLECTION_NAME

    client = _get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return {"tags": []}

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
            if p.payload and "tags" in p.payload:
                all_tags.update(p.payload["tags"])
        if offset is None:
            break

    return {"tags": sorted(all_tags)}


@app.post("/index-all", summary="索引全部文件", description="索引 data/upload/ 目录下的所有文件")
async def index_all(user: dict = Depends(require_user)):
    from rag.folder_indexer import index_folder
    global pipeline
    require_admin(user)
    if not DATA_DIR.is_dir():
        raise HTTPException(status_code=400, detail="data/upload/ 目录不存在")
    try:
        stats = await asyncio.to_thread(index_folder, str(DATA_DIR))
    except Exception as exc:
        logger.exception("full reindex failed", extra={"user_id": user["id"]})
        raise HTTPException(status_code=400, detail="全量索引失败") from exc
    with _pipeline_lock.write():
        try:
            pipeline = await asyncio.to_thread(_get_or_create_pipeline, "rag_docs")
        except Exception as e:
            logger.error("Pipeline 刷新失败（全量索引后）: %s", e)
    return {"status": "indexed", **stats}


@app.post("/index", summary="索引文档", description="上传文档并添加到知识库，支持 txt/md/pdf/docx/xlsx 格式，最大 10MB")
async def index(
    file: UploadFile = File(..., description="要索引的文档文件"),
    duplicate_action: str = Form("reject"),
    user: dict = Depends(require_user),
):
    from rag.pipeline import RAGPipeline
    global pipeline
    content = await file.read(MAX_FILE_SIZE + 1)
    result = await _ingest_default_document(
        content=content,
        filename=file.filename or "upload.txt",
        user=user,
        db=user_db,
        duplicate_action=duplicate_action,
    )
    # 刷新 pipeline（冷启动时创建，运行时更新）
    with _pipeline_lock.write():
        try:
            pipeline = RAGPipeline(kb_id="rag_docs", user_db=user_db)
        except Exception:
            logger.exception("Pipeline refresh failed after index")
    return {**result, "status": "indexed"}


@app.post("/batch-import", summary="批量导入", description="上传 Excel/CSV 批量导入到知识库")
async def batch_import(
    file: UploadFile = File(...),
    mode: str = "qa_pair",
    config: str = "{}",
    user: dict = Depends(require_user),
):
    import json as _json
    from rag.batch_importer import BatchImporter

    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件过大")

    suffix = Path(file.filename).suffix.lower() if file.filename else ".csv"
    if suffix not in {".csv", ".xlsx"}:
        raise HTTPException(status_code=400, detail=f"不支持的格式: {suffix}")

    doc_name = Path(file.filename or "upload").stem
    if await asyncio.to_thread(user_db.get_document_permission, doc_name, "rag_docs"):
        raise HTTPException(status_code=409, detail="同名文件已存在")

    # 保存临时文件
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(content)
        tmp_path = tmp.name
    finally:
        tmp.close()

    try:
        parsed_config = _json.loads(config)
    except _json.JSONDecodeError:
        parsed_config = {}

    try:
        importer = BatchImporter()
        chunks = importer.parse(
            tmp_path, mode=mode, config=parsed_config, doc_name=doc_name
        )

        if not chunks:
            raise HTTPException(status_code=400, detail="解析结果为空")

        # 索引到知识库
        from rag.embedder import embed
        from rag.vector_store import _ensure_collection_name, add_to_collection, delete_doc

        embeddings = embed([c.text for c in chunks])
        permission_id = await asyncio.to_thread(
            user_db.create_document_permission,
            doc_name,
            "rag_docs",
            user["id"],
            1,
            False,
        )
        try:
            await asyncio.to_thread(_ensure_collection_name, "rag_docs")
            await asyncio.to_thread(
                add_to_collection,
                "rag_docs",
                chunks,
                embeddings,
                doc_permission_id=permission_id,
            )
        except Exception:
            await asyncio.to_thread(user_db.delete_document_permission, permission_id)
            try:
                await asyncio.to_thread(delete_doc, "rag_docs", doc_name)
            except Exception:
                logger.exception("batch import vector rollback failed", extra={"doc_name": doc_name})
            raise

        # 刷新 pipeline
        global pipeline
        with _pipeline_lock.write():
            try:
                pipeline = _get_or_create_pipeline("rag_docs")
            except Exception as e:
                logger.error("Pipeline 刷新失败: %s", e)

        return {"status": "imported", "chunks": len(chunks), "mode": mode}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="批量导入失败") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("batch import failed", extra={"user_id": user["id"], "doc_name": doc_name})
        raise HTTPException(status_code=400, detail="批量导入失败") from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _record_refusal_task(
    db: UserDB,
    user: dict,
    question: str,
    kb_id: str,
    doc_name: str | None,
) -> None:
    """Record an insufficient-evidence report without linking an invisible document."""
    document_id = None
    if doc_name:
        document = db.get_document_permissions_by_names([doc_name], kb_id).get(doc_name)
        if document is not None:
            from rag.permissions import check_doc_permission

            try:
                check_doc_permission(db, document["doc_name"], kb_id, user, "view")
            except HTTPException:
                pass
            else:
                document_id = document["id"]
    try:
        from rag.knowledge_tasks import TaskService

        TaskService(db).record_refusal(int(user["id"]), question, document_id)
    except Exception:
        logger.exception("failed to record insufficient-evidence task")


@app.post("/query", response_model=QueryResponse, summary="查询知识库", description="对已索引的文档进行语义检索问答")
async def query(req: QueryRequest, user: dict = Depends(require_user)):
    global pipeline
    with _pipeline_lock.read():
        if pipeline is None:
            return JSONResponse(
                status_code=400,
                content={"error": "尚未索引文档，请先调用 POST /index 上传文档"},
            )
        current_pipeline = pipeline

    # Determine session_id: use conversation-based session if user + conversation_id provided
    session_id = req.session_id
    if user and req.conversation_id is not None:
        session_id = f"conv_{req.conversation_id}"

    result = await asyncio.to_thread(
        contextvars.copy_context().run,
        current_pipeline.query,
        req.question,
        top_k=req.top_k,
        session_id=session_id,
        doc_name=req.doc_name,
        tags=req.tags,
        user=user,
        kb_id="rag_docs",
    )
    if getattr(getattr(result, "decision", None), "kind", None) == "insufficient_evidence":
        await asyncio.to_thread(
            _record_refusal_task, user_db, user, req.question, "rag_docs", req.doc_name
        )

    # Save messages to chat_messages if user and conversation_id are provided
    if user and req.conversation_id is not None:
        # 验证 conversation 归属
        conv = await asyncio.to_thread(user_db.get_conversation, req.conversation_id, user["id"])
        if conv:
            await asyncio.to_thread(user_db.add_message, req.conversation_id, "user", req.question)
            reliability_kwargs = {}
            confidence = getattr(result, "confidence", None)
            decision = getattr(result, "decision", None)
            if confidence is not None and decision is not None:
                reliability_kwargs = {
                    "confidence_level": confidence.level,
                    "confidence_reasons": confidence.reasons,
                    "answerability_kind": decision.kind,
                }
            await asyncio.to_thread(
                user_db.add_message,
                req.conversation_id,
                "assistant",
                result.answer,
                **reliability_kwargs,
            )
        else:
            logger.warning("对话 %s 不存在或不属于用户 %s，跳过消息保存", req.conversation_id, user["id"])

    return QueryResponse(answer=result.answer, sources=result.sources)


@app.post("/query/stream", summary="流式查询知识库", description="SSE 流式返回查询结果")
async def query_stream(
    req: StreamQueryRequest,
    user: dict = Depends(require_user),
):
    global pipeline
    target_kb = req.kb_id or "rag_docs"
    if target_kb != "rag_docs":
        require_kb_view(user_db, target_kb, user)
        from rag.pipeline import RAGPipeline
        current_pipeline = await asyncio.to_thread(
            RAGPipeline, kb_id=target_kb, user_db=user_db
        )
    else:
        with _pipeline_lock.read():
            if pipeline is None:
                return JSONResponse(status_code=400, content={"error": "尚未索引文档"})
            current_pipeline = pipeline

    session_id = req.session_id
    if user and req.conversation_id is not None:
        session_id = f"conv_{req.conversation_id}"

    async def event_generator():
        import json as _json

        from rag.citations import CitationRecord

        answer_buffer = ""
        citation_records: list[CitationRecord] = []
        reliability = None
        try:
            async for event in current_pipeline.query_stream(
                req.question, top_k=req.top_k, session_id=session_id, doc_name=req.doc_name, tags=req.tags,
                user=user, kb_id=target_kb,
            ):
                # 从 token 事件中提取 answer 用于持久化
                if '"type": "token"' in event:
                    try:
                        data = _json.loads(event.removeprefix("data: ").strip())
                        answer_buffer += data.get("content", "")
                    except Exception:
                        pass
                elif '"type": "sources"' in event:
                    try:
                        data = _json.loads(event.removeprefix("data: ").strip())
                        for source in data.get("sources", []):
                            if source.get("document_id") and source.get("document_version_id"):
                                citation_records.append(
                                    CitationRecord(
                                        document_id=source["document_id"],
                                        document_version_id=source["document_version_id"],
                                        chunk_index=source.get("chunk_index", 0),
                                        section_title=source.get("section_title") or "",
                                        text_preview=source.get("text_preview", "")[:200],
                                        rank=source.get("rank", 0),
                                        is_historical=bool(source.get("is_historical", False)),
                                    )
                                )
                    except Exception:
                        pass
                elif '"type": "done"' in event:
                    # The pipeline's final done is replaced with an enriched one below.
                    try:
                        data = _json.loads(event.removeprefix("data: ").strip())
                        reliability = data.get("reliability")
                    except Exception:
                        reliability = None
                    continue
                yield event
        except Exception:
            logger.exception("流式查询中途失败")
            payload = {"type": "error", "reason": "流式生成失败，请稍后重试"}
            yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        # 流式完成后持久化 assistant 消息与引用（一个事务内）
        assistant_message_id = None
        assistant_message_id = None
        if reliability and reliability.get("answerability_kind") == "insufficient_evidence":
            await asyncio.to_thread(
                _record_refusal_task,
                user_db,
                user,
                req.question,
                target_kb,
                req.doc_name,
            )
        if user and req.conversation_id is not None and answer_buffer:
            conv = await asyncio.to_thread(user_db.get_conversation, req.conversation_id, user["id"])
            if conv:
                await asyncio.to_thread(user_db.add_message, req.conversation_id, "user", req.question)
                persistence_key = f"stream:{req.conversation_id}:{hashlib.sha256(req.question.encode('utf-8')).hexdigest()[:16]}"
                assistant_message_id = await asyncio.to_thread(
                    user_db.persist_assistant_answer_with_citations,
                    req.conversation_id,
                    answer_buffer,
                    citation_records,
                    persistence_key,
                    **(reliability or {}),
                )

            else:
                logger.warning("对话 %s 不存在或不属于用户 %s，跳过消息保存", req.conversation_id, user["id"])
        yield f"data: {_json.dumps({'type': 'done', 'assistant_message_id': assistant_message_id})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/suggest", summary="生成追问建议", description="基于问答生成 3 个推荐追问")
async def suggest(req: SuggestRequest, user_id: str = Security(verify_api_key)):
    from rag.suggest import suggest_questions
    questions = await asyncio.to_thread(suggest_questions, req.question, req.answer)
    return {"questions": questions}


class BatchImportRequest(BaseModel):
    mode: str = Field(pattern="^(qa_pair|document|table)$")
    config: dict = Field(default_factory=dict)


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


@app.get("/knowledge-bases", summary="列出知识库", description="获取所有知识库列表")
async def list_knowledge_bases(user: dict = Depends(require_user)):
    from rag.org_auth.repository import OrgRepository
    from rag.org_auth.service import OrgAuthorizationService
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


@app.post("/knowledge-bases", summary="创建知识库", description="创建一个新的知识库")
async def create_knowledge_base(req: CreateKBRequest, user: dict = Depends(require_user)):

    from rag.org_auth.repository import OrgRepository
    from rag.org_auth.service import OrgAuthorizationService
    org_repo = OrgRepository(user_db)
    if org_repo.get_person_for_user(int(user["id"])) is not None:
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
        return {"kb_id": kb_id, "name": req.name, "kb_type": "organization", "org_unit_code": req.org_unit_code, "effective_role": "manage"}

    def _create():
        manager = KnowledgeBaseManager()
        kb_id = manager.create_kb(req.name)
        user_db.create_kb_metadata(kb_id, req.name, owner_id=user["id"], scope=req.scope)
        return kb_id

    kb_id = await asyncio.to_thread(_create)
    return {"kb_id": kb_id, "name": req.name, "scope": req.scope}


class KBScopeRequest(BaseModel):
    scope: str = Field(..., pattern="^(private|public)$", description="新的可见范围")


@app.put("/knowledge-bases/{kb_id}/scope", summary="切换知识库可见范围")
async def update_kb_scope(
    kb_id: str,
    req: KBScopeRequest,
    user: dict = Depends(require_user),
):
    from rag.org_auth.repository import OrgRepository
    if OrgRepository(user_db).get_kb_policy(kb_id) is not None:
        raise HTTPException(status_code=404, detail="资源不存在")
    require_kb_edit(user_db, kb_id, user)
    if req.scope == "private" and await asyncio.to_thread(user_db.list_kb_shared_users, kb_id):
        raise HTTPException(status_code=409, detail="请先取消所有共享后再设为私有")

    try:
        await asyncio.to_thread(user_db.update_kb_scope, kb_id, req.scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"kb_id": kb_id, "scope": req.scope}


@app.delete("/knowledge-bases/{kb_id}", summary="删除知识库", description="删除指定知识库及其所有文档")
async def delete_knowledge_base(kb_id: str, user: dict = Depends(require_user)):
    if kb_id == "rag_docs":
        raise HTTPException(status_code=400, detail="不能删除系统集合 rag_docs")

    require_kb_edit(user_db, kb_id, user)

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
                stored = resolve_stored_path(KB_FILES_ROOT, record["file_path"])
                stored.unlink(missing_ok=True)
                try:
                    stored.parent.rmdir()
                except OSError:
                    pass
            await asyncio.to_thread(user_db.delete_kb_document, record["id"])
        manager.delete_kb(kb_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在")
    # Clean up SQLite metadata
    def _cleanup():
        with user_db.locked_transaction() as conn:
            conn.execute("DELETE FROM kb_documents WHERE kb_id = ?", (kb_id,))
            conn.execute("DELETE FROM kb_org_policies WHERE kb_id = ?", (kb_id,))
            conn.execute("DELETE FROM kb_metadata WHERE kb_id = ?", (kb_id,))
    await asyncio.to_thread(_cleanup)
    return {"status": "deleted", "kb_id": kb_id}


@app.post("/knowledge-bases/{kb_id}/documents", summary="添加文档到知识库", description="上传文档到指定知识库")
async def add_document_to_kb(
    kb_id: str,
    file: UploadFile = File(...),
    duplicate_action: str = Form("reject"),
    user: dict = Depends(require_user),
):
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
        # Reserve the kb_documents lifecycle row for the resolved final name.
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
            _versions_root(),
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
            from rag.spreadsheets.migration import ensure_structured_dataset

            spreadsheet_format = Path(result.filename).suffix.lower().removeprefix(".")
            try:
                await asyncio.to_thread(
                    ensure_structured_dataset,
                    user_db,
                    Path(
                        getattr(
                            app.state,
                            "structured_datasets_root",
                            settings.structured_datasets_root,
                        )
                    ),
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

    return {
        "status": "added",
        "kb_id": kb_id,
        "chunks": result.chunks,
        "document_id": result.document_id,
        "version_id": result.version_id,
        "version_no": result.version_no,
    }


@app.delete("/knowledge-bases/{kb_id}/documents/{doc_name}", summary="删除文档", description="从知识库中删除指定文档")
async def remove_document_from_kb(
    kb_id: str, doc_name: str, user: dict = Depends(require_user)
):
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
            stored = resolve_stored_path(KB_FILES_ROOT, record["file_path"])
            stored.unlink(missing_ok=True)
            try:
                stored.parent.rmdir()
            except OSError:
                pass
        await asyncio.to_thread(user_db.delete_kb_document, record["id"])
    except Exception as exc:
        await asyncio.to_thread(
            user_db.set_kb_document_state, record["id"], "delete_failed", str(exc)
        )
        logger.exception("KB document deletion failed", extra={"kb_id": kb_id})
        raise HTTPException(status_code=400, detail="文档删除失败") from exc

    return {"status": "removed", "kb_id": kb_id, "doc_name": doc_name}


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


class ShareRequest(BaseModel):
    user_id: int = Field(..., description="被共享用户 ID")
    permission: str = Field(default="view", pattern="^(view|edit)$", description="权限：view 或 edit")


@app.post("/knowledge-bases/{kb_id}/query", summary="查询知识库", description="对指定知识库进行语义检索")
async def query_knowledge_base(
    kb_id: str, req: QueryKBRequest, user: dict = Depends(require_user)
):
    require_kb_view(user_db, kb_id, user)

    manager = KnowledgeBaseManager()
    try:
        chunks = manager.search(kb_id, req.question, top_k=req.top_k * 3)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    visible_chunks = await asyncio.to_thread(
        filter_chunks_by_permission, user_db, kb_id, chunks, user
    )
    results = [
        {"doc_name": c.doc_name, "chunk_index": c.chunk_index, "text": c.text}
        for c in visible_chunks[:req.top_k]
    ]
    return {"kb_id": kb_id, "results": results}


@app.get("/knowledge-bases/{kb_id}", summary="获取知识库详情")
async def get_knowledge_base_detail(
    kb_id: str,
    user: dict = Depends(require_user),
):
    """获取知识库详情，包括元数据、文档列表、概述、目录。"""
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


@app.put("/knowledge-bases/{kb_id}/overview", summary="更新知识库概述")
async def update_kb_overview(
    kb_id: str, req: KBOverviewRequest, user: dict = Depends(require_user)
):
    require_kb_edit(user_db, kb_id, user)
    updated = await asyncio.to_thread(user_db.update_kb_overview, kb_id, req.overview)
    if not updated:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"status": "updated"}


@app.put("/knowledge-bases/{kb_id}/name", summary="重命名知识库")
async def rename_knowledge_base(kb_id: str, req: KBRenameRequest, authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)
    _check_kb_edit_permission(kb_id, user_dict)

    def _update():
        with user_db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO kb_metadata (kb_id, name) VALUES (?, ?) "
                "ON CONFLICT(kb_id) DO UPDATE SET name = ?",
                (kb_id, req.name, req.name),
            )

    await asyncio.to_thread(_update)
    return {"status": "updated", "name": req.name}


@app.put("/knowledge-bases/{kb_id}/documents/{doc_name}/toc", summary="更新文档目录")
async def update_doc_toc(
    kb_id: str, doc_name: str, req: KBTocRequest, user: dict = Depends(require_user)
):
    document = await asyncio.to_thread(user_db.get_kb_document, kb_id, doc_name)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_doc_edit(user_db, doc_name, kb_id, user)

    import json
    def _update():
        toc_str = json.dumps(req.toc, ensure_ascii=False)
        return user_db.update_kb_document_toc(kb_id, doc_name, toc_str)

    result = await asyncio.to_thread(_update)
    if not result:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"status": "updated"}


@app.put("/knowledge-bases/{kb_id}/documents/{doc_name}/summary", summary="更新文档概述")
async def update_doc_summary(
    kb_id: str, doc_name: str, req: KBSummaryRequest, user: dict = Depends(require_user)
):
    document = await asyncio.to_thread(user_db.get_kb_document, kb_id, doc_name)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_doc_edit(user_db, doc_name, kb_id, user)

    await asyncio.to_thread(user_db.update_kb_document_summary, kb_id, doc_name, req.summary)
    return {"status": "updated"}


@app.post("/knowledge-bases/{kb_id}/toc/generate", summary="LLM 生成知识库目录")
async def generate_kb_toc(kb_id: str, authorization: str = Header(default="")):
    """对知识库中所有文档调用 LLM 生成目录结构，并保存到 kb_documents.toc。"""
    user_dict = await _require_auth(authorization)
    _check_kb_edit_permission(kb_id, user_dict)
    def _generate():
        import json as _json
        try:
            with user_db.locked_read() as conn:
                docs = conn.execute(
                    "SELECT filename FROM kb_documents WHERE kb_id = ? AND status = 'indexed'",
                    (kb_id,),
                ).fetchall()
            if not docs:
                return None, "no_documents"

            toc_results = {}
            for doc in docs:
                filename = doc["filename"]
                # 使用 loader 解析文档内容（支持 docx/xlsx/pdf 等二进制格式）
                file_path = DATA_DIR / filename
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


@app.post("/knowledge-bases/{kb_id}/overview/generate", summary="LLM 生成知识库概述")
async def generate_kb_overview(kb_id: str, authorization: str = Header(default="")):
    """对知识库中所有文档内容调用 LLM 生成概述，并保存到 kb_metadata.overview。"""
    user_dict = await _require_auth(authorization)
    _check_kb_edit_permission(kb_id, user_dict)
    def _generate():
        try:
            with user_db.locked_read() as conn:
                docs = conn.execute(
                    "SELECT filename FROM kb_documents WHERE kb_id = ? AND status = 'indexed'",
                    (kb_id,),
                ).fetchall()
            if not docs:
                return None, "no_documents"

            # 合并所有文档内容（使用 loader 解析，支持二进制格式）
            all_content = []
            for doc in docs:
                filename = doc["filename"]
                file_path = DATA_DIR / filename
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


# ── 共享管理端点（Phase 2）────────────────────────────────────────────


@app.post("/knowledge-bases/{kb_id}/share", summary="共享知识库给指定用户")
async def share_knowledge_base(kb_id: str, req: ShareRequest, authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)
    from rag.org_auth.repository import OrgRepository
    if OrgRepository(user_db).get_kb_policy(kb_id) is not None:
        raise HTTPException(status_code=404, detail="资源不存在")

    # Owner 检查
    meta = user_db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not user_dict.get("is_admin") and meta["owner_id"] != user_dict["id"]:
        raise HTTPException(status_code=403, detail="仅知识库所有者或管理员可共享")

    # 检查目标用户是否存在
    target = user_db.get_user_by_id(req.user_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"用户 ID {req.user_id} 不存在")

    try:
        await asyncio.to_thread(user_db.share_kb, kb_id, req.user_id, user_dict["id"], req.permission)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail="该用户已被共享")
        raise HTTPException(status_code=500, detail=str(e))

    return {"kb_id": kb_id, "user_id": req.user_id, "permission": req.permission}


@app.delete("/knowledge-bases/{kb_id}/share/{user_id}", summary="取消知识库共享", status_code=204)
async def unshare_knowledge_base(kb_id: str, user_id: int, authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)
    from rag.org_auth.repository import OrgRepository
    if OrgRepository(user_db).get_kb_policy(kb_id) is not None:
        raise HTTPException(status_code=404, detail="资源不存在")

    # Owner 检查
    meta = user_db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not user_dict.get("is_admin") and meta["owner_id"] != user_dict["id"]:
        raise HTTPException(status_code=403, detail="仅知识库所有者或管理员可操作")

    await asyncio.to_thread(user_db.unshare_kb, kb_id, user_id)
    return None


@app.get("/knowledge-bases/{kb_id}/shares", summary="查看知识库共享列表")
async def list_kb_shares(kb_id: str, authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)
    from rag.org_auth.repository import OrgRepository
    if OrgRepository(user_db).get_kb_policy(kb_id) is not None:
        raise HTTPException(status_code=404, detail="资源不存在")

    # Owner 检查
    meta = user_db.get_kb_metadata(kb_id)
    if not meta:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not user_dict.get("is_admin") and meta["owner_id"] != user_dict["id"]:
        raise HTTPException(status_code=403, detail="仅知识库所有者或管理员可查看")

    shares = await asyncio.to_thread(user_db.list_kb_shared_users, kb_id)
    return shares


# ── 权限管理端点 ──────────────────────────────────────────────────────


@app.get("/documents/{doc_id}/permissions", summary="查看文档权限信息")
async def get_document_permissions(doc_id: int, authorization: str = Header(default="")):
    # C1: 要求认证
    user_dict = await _require_auth(authorization)

    def _get():
        perm = user_db.get_document_permission_by_id(doc_id)
        if not perm:
            return None
        require_personal_document_acl(user_db, perm["kb_id"])
        # 检查查看权限：admin / owner / 共享用户 / 公开文档
        if (not user_dict.get("is_admin")
                and perm["owner_id"] != user_dict["id"]
                and not (perm.get("scope") == "shared"
                         and user_db.is_document_shared(doc_id, user_dict["id"]))
                and perm.get("scope") != "public"):
            raise HTTPException(status_code=403, detail="无权查看该文档权限信息")
        shared_users = user_db.list_shared_users(doc_id)
        owner = user_db.get_user_by_id(perm["owner_id"])
        return {
            "doc_id": doc_id,
            "doc_name": perm["doc_name"],
            "is_public": perm.get("scope") == "public",
            "protected": perm.get("protected", False),
            "owner": {"id": owner["id"], "username": owner["username"]} if owner else None,
            "shared_with": shared_users,
        }

    try:
        result = await asyncio.to_thread(_get)
    except HTTPException:
        raise
    if not result:
        raise HTTPException(status_code=404, detail="文档权限记录不存在")
    return result


@app.put("/documents/{doc_id}/permission", summary="切换文档公开/私有")
async def update_document_permission(doc_id: int, authorization: str = Header(default="")):
    """切换文档的公开/私有状态（仅 owner 或 admin 可操作）。"""
    user_dict = await _require_auth(authorization)

    def _toggle():
        perm = user_db.get_document_permission_by_id(doc_id)
        if not perm:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_personal_document_acl(user_db, perm["kb_id"])
        require_doc_edit(user_db, perm["doc_name"], perm["kb_id"], user_dict)
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文档上传者或管理员可修改权限")
        new_val = user_db.toggle_document_visibility(doc_id)
        return {"id": doc_id, "is_public": new_val}

    try:
        return await asyncio.to_thread(_toggle)
    except HTTPException:
        raise


@app.post("/documents/{doc_id}/share", summary="共享文档给指定用户")
async def share_document(doc_id: int, req: ShareRequest, authorization: str = Header(default="")):
    import sqlite3 as _sqlite3
    # C1: 要求认证
    user_dict = await _require_auth(authorization)

    def _share():
        perm = user_db.get_document_permission_by_id(doc_id)
        if not perm:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_personal_document_acl(user_db, perm["kb_id"])
        require_doc_edit(user_db, perm["doc_name"], perm["kb_id"], user_dict)
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文档上传者或管理员可共享")
        # I4: 检查目标用户是否存在
        target = user_db.get_user_by_id(req.user_id)
        if not target:
            raise HTTPException(status_code=404, detail=f"用户 ID {req.user_id} 不存在")
        try:
            user_db.share_document(doc_id, req.user_id, user_dict["id"], req.permission)
        except _sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="该用户已被共享")
        return {
            "doc_id": doc_id,
            "user_id": req.user_id,
            "granted_by": user_dict["id"],
            "permission": req.permission,
        }

    try:
        return await asyncio.to_thread(_share)
    except HTTPException:
        raise


@app.delete("/documents/{doc_id}/share/{user_id}", summary="撤销文档共享", status_code=204)
async def unshare_document(doc_id: int, user_id: int, authorization: str = Header(default="")):
    # C1: 要求认证
    user_dict = await _require_auth(authorization)

    def _unshare():
        perm = user_db.get_document_permission_by_id(doc_id)
        if not perm:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_personal_document_acl(user_db, perm["kb_id"])
        require_doc_edit(user_db, perm["doc_name"], perm["kb_id"], user_dict)
        if not user_dict.get("is_admin") and perm["owner_id"] != user_dict["id"]:
            raise HTTPException(status_code=403, detail="仅文档上传者或管理员可撤销共享")
        user_db.unshare_document(doc_id, user_id)

    try:
        await asyncio.to_thread(_unshare)
    except HTTPException:
        raise
    return None


@app.put("/users/{uid}/role", summary="设置用户角色（管理员/权限等级）")
async def set_user_role(uid: int, req: UpdateRoleRequest, authorization: str = Header(default="")):
    user_dict = await _require_auth(authorization)
    if not user_dict.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    def _set():
        target = user_db.get_user_by_id(uid)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        if req.is_admin is not None:
            user_db.set_user_admin(uid, req.is_admin)
        return user_db.get_user_by_id(uid)

    try:
        return await asyncio.to_thread(_set)
    except HTTPException:
        raise


# ── 分析端点 ──────────────────────────────────────────────────────

@app.get("/analytics/gaps/summary", summary="检索空白分析统计")
async def get_gap_summary(user_id: str = Security(verify_api_key)):
    return await asyncio.to_thread(get_gap_analyzer().get_summary)


@app.get("/analytics/gaps", summary="未解答问题列表")
async def get_gaps(limit: int = 50, user_id: str = Security(verify_api_key)):
    return await asyncio.to_thread(get_gap_analyzer().get_gaps, limit=limit)


# ── 分析卡片 ────────────────────────────────────────────────────────


@app.get("/analysis/cards", summary="获取分析卡片列表")
async def list_analysis_cards(authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return await asyncio.to_thread(user_db.list_cards, user["id"])


@app.post("/analysis/cards", summary="创建分析卡片")
async def create_analysis_card(req: CreateCardRequest, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    card_id = await asyncio.to_thread(user_db.create_card, user["id"], req.name)
    return {"id": card_id, "name": req.name}


@app.get("/analysis/cards/{card_id}/questions", summary="获取卡片问题列表")
async def list_analysis_questions(card_id: int, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    # Verify card ownership
    cards = await asyncio.to_thread(user_db.list_cards, user["id"])
    if not any(c["id"] == card_id for c in cards):
        raise HTTPException(status_code=404, detail="卡片不存在或无权操作")
    return await asyncio.to_thread(user_db.get_questions, card_id)


@app.delete("/analysis/cards/{card_id}", summary="删除分析卡片")
async def delete_analysis_card(card_id: int, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    deleted = await asyncio.to_thread(user_db.delete_card, card_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return {"status": "deleted"}


@app.put("/analysis/cards/{card_id}/name", summary="重命名分析卡片")
async def rename_analysis_card(card_id: int, req: RenameCardRequest, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    updated = await asyncio.to_thread(user_db.rename_card, card_id, req.name, user["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return {"id": card_id, "name": req.name}


@app.post("/analysis/cards/{card_id}/questions", summary="添加分析问题")
async def add_analysis_question(card_id: int, req: AddQuestionRequest, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    qid = await asyncio.to_thread(
        user_db.add_question,
        card_id,
        req.question,
        req.answer,
        req.source_mode,
        req.source_message_id,
        user["id"],
    )
    if qid is None:
        raise HTTPException(status_code=404, detail="卡片不存在或无权操作")
    return {"id": qid, "question": req.question, "answer": req.answer}


@app.delete("/analysis/cards/{card_id}/questions/{qid}", summary="删除分析问题")
async def delete_analysis_question(card_id: int, qid: int, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    deleted = await asyncio.to_thread(user_db.delete_question, qid, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="问题不存在")
    return {"status": "deleted"}


# ── 分析卡片摘要 ────────────────────────────────────────────────────────


def _generate_summary_llm(card_id: int, questions: list[dict]) -> str:
    """Generate a summary for an analysis card using the LLM.

    This is a module-level function so tests can monkeypatch it.
    """
    from rag.generator import generate

    q_text = "\n".join(
        f"- {q['question']}" + (f"\n  回答: {q['answer']}" if q.get("answer") else "")
        for q in questions
    )
    prompt = (
        "请根据以下问题和回答，为这组分析卡片生成一段简洁的中文摘要（100-200字），"
        "概括这组问题的主题和核心内容。\n\n"
        f"问题列表：\n{q_text}"
    )
    return generate([{"role": "user", "content": prompt}])


@app.get("/analysis/cards/{card_id}/summary", summary="获取卡片摘要")
async def get_card_summary(card_id: int, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    cards = await asyncio.to_thread(user_db.list_cards, user["id"])
    if not any(c["id"] == card_id for c in cards):
        raise HTTPException(status_code=404, detail="卡片不存在")
    summary = await asyncio.to_thread(user_db.get_card_summary, card_id)
    return {"summary": summary}


@app.put("/analysis/cards/{card_id}/summary", summary="更新卡片摘要")
async def update_card_summary(card_id: int, req: UpdateSummaryRequest, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    updated = await asyncio.to_thread(user_db.update_card_summary, card_id, req.summary, user["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return {"summary": req.summary}


@app.post("/analysis/cards/{card_id}/summary/generate", summary="LLM 生成卡片摘要")
async def generate_card_summary(card_id: int, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    cards = await asyncio.to_thread(user_db.list_cards, user["id"])
    if not any(c["id"] == card_id for c in cards):
        raise HTTPException(status_code=404, detail="卡片不存在")
    questions = await asyncio.to_thread(user_db.get_questions, card_id)
    if not questions:
        raise HTTPException(status_code=400, detail="卡片中没有问题，无法生成摘要")
    summary = await asyncio.to_thread(_generate_summary_llm, card_id, questions)
    await asyncio.to_thread(user_db.update_card_summary, card_id, summary, user["id"])
    return {"summary": summary}


# ── 分析卡片导出 ────────────────────────────────────────────────────────


def _build_card_markdown(card_name: str, summary: str, questions: list[dict]) -> str:
    """Build a Markdown representation of an analysis card.

    This is a module-level function so tests can call it directly.
    """
    lines = [f"# {card_name}"]
    if summary:
        lines.append("")
        lines.append(f"> {summary}")
    lines.append("")
    lines.append("## 问题列表")
    lines.append("")
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q['question']}")
        if q.get("answer"):
            lines.append(f"   - 回答：{q['answer']}")
        if q.get("source_mode"):
            lines.append(f"   - 来源：{q['source_mode']}")
        if q.get("created_at"):
            lines.append(f"   - 添加时间：{q['created_at']}")
    return "\n".join(lines) + "\n"


@app.get("/analysis/cards/{card_id}/export", summary="导出分析卡片")
async def export_analysis_card(
    card_id: int,
    format: str = "markdown",
    authorization: str = Header(default=""),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    cards = await asyncio.to_thread(user_db.list_cards, user["id"])
    card = next((c for c in cards if c["id"] == card_id), None)
    if not card:
        raise HTTPException(status_code=404, detail="卡片不存在")
    if format != "markdown":
        raise HTTPException(status_code=400, detail=f"不支持的导出格式：{format}")
    questions = await asyncio.to_thread(user_db.get_questions, card_id)
    summary = await asyncio.to_thread(user_db.get_card_summary, card_id)
    md = _build_card_markdown(card["name"], summary, questions)
    return PlainTextResponse(content=md, media_type="text/markdown; charset=utf-8")


# ── 问题归入卡片建议 ────────────────────────────────────────────────────


def _suggest_card_llm(question: str, answer: str, cards: list[dict]) -> dict:
    """Use LLM to suggest which card a question belongs to.

    Returns {"suggested_card_id": int|None, "confidence": float}.
    This is a module-level function so tests can monkeypatch it.
    """
    from rag.generator import generate

    card_names = "\n".join(f"- id={c['id']}: {c['name']}" for c in cards)
    prompt = (
        "你是一个分析助手。根据以下问题和回答，判断它最应该归入哪个已有的分析卡片。\n\n"
        f"已有卡片：\n{card_names}\n\n"
        f"问题：{question}\n回答：{answer}\n\n"
        "请返回 JSON 格式：{\"card_id\": <id 或 null>, \"confidence\": <0-1 之间的浮点数>}\n"
        "如果没有合适的卡片，card_id 返回 null，confidence 返回 0。"
    )
    result = generate([{"role": "user", "content": prompt}])
    import json as _json
    import re

    match = re.search(r'\{.*\}', result, re.DOTALL)
    if match:
        try:
            parsed = _json.loads(match.group())
            return {
                "suggested_card_id": parsed.get("card_id"),
                "confidence": float(parsed.get("confidence", 0)),
            }
        except (ValueError, KeyError):
            pass
    return {"suggested_card_id": None, "confidence": 0}


@app.post("/analysis/suggest-card", summary="建议问题归入哪个卡片")
async def suggest_card(req: SuggestCardRequest, authorization: str = Header(default="")):
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "")
    user = await asyncio.to_thread(_get_current_user, token)
    if not user:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    cards = await asyncio.to_thread(user_db.list_cards, user["id"])
    if not cards:
        return {
            "suggested_card_id": None,
            "suggested_card_name": None,
            "confidence": 0,
            "all_cards": [],
        }
    # Fetch questions for each card for LLM context
    cards_with_questions = []
    for c in cards:
        qs = await asyncio.to_thread(user_db.get_questions, c["id"])
        cards_with_questions.append({**c, "questions": qs})
    suggestion = await asyncio.to_thread(_suggest_card_llm, req.question, req.answer, cards_with_questions)
    suggested_id = suggestion["suggested_card_id"]
    suggested_name = None
    if suggested_id is not None:
        for c in cards:
            if c["id"] == suggested_id:
                suggested_name = c["name"]
                break
    return {
        "suggested_card_id": suggested_id,
        "suggested_card_name": suggested_name,
        "confidence": suggestion["confidence"],
        "all_cards": [{"id": c["id"], "name": c["name"]} for c in cards],
    }


# ── 数据源管理 ────────────────────────────────────────────────────────

import json as _json


class CreateSourceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="数据源名称")
    type: str = Field(..., pattern="^(rss|database|api)$", description="数据源类型")
    config: dict = Field(default_factory=dict, description="数据源配置")


class SourceResponse(BaseModel):
    id: int
    name: str
    type: str
    config: dict
    has_secret: bool = False
    status: str
    last_synced_at: float | None
    created_at: float


def _resolve_source_owner(user_id: str, authorization: str) -> str:
    """把 /sources 端点的鉴权信息解析为数据源 owner_id（TEXT）。

    - 优先用 JWT（Authorization: Bearer）解析真实用户 id（含 admin 判定）。
    - 否则退回 ``verify_api_key`` 返回的 user_id 字符串（API key 路径）。
    - 认证关闭 / 匿名（user_id 为 'anonymous' 或空）时视为独立的非 admin
      owner ``'anonymous'``：匿名者只能看到/删除/同步自己（认证关闭期间）
      创建的、owner 为 ``'anonymous'`` 的数据源；预置的 ``'admin'``/他人数据源
      对匿名者不可见、不可删、不可同步。
    """
    token = (
        authorization.replace("Bearer ", "")
        if authorization.startswith("Bearer ")
        else ""
    )
    if token:
        user = _get_current_user(token)
        if user:
            return str(user["id"])
    if user_id and user_id not in ("", "anonymous"):
        return str(user_id)
    return "anonymous"


@app.post("/sources", summary="创建数据源", description="创建新的数据源（RSS / 数据库 / API）")
async def create_source(req: CreateSourceRequest, user: dict = Depends(require_data_sources_admin)):
    source_id = await asyncio.to_thread(
        user_db.create_data_source,
        req.name,
        req.type,
        _json.dumps(req.config, ensure_ascii=False),
        str(user["id"]),
    )
    return {
        "id": source_id,
        "name": req.name,
        "type": req.type,
        "config": redact_source_config(req.config),
        "status": "inactive",
        "last_synced_at": None,
    }


@app.get("/sources", summary="列出数据源", description="获取所有数据源列表")
async def list_sources(user: dict = Depends(require_data_sources_admin)):
    sources = await asyncio.to_thread(user_db.list_data_sources)
    result = []
    for s in sources:
        config = (
            _json.loads(s["config"])
            if isinstance(s["config"], str) and s["config"]
            else (s["config"] or {})
        )
        redacted_config = redact_source_config(config)
        result.append({
            "id": s["id"],
            "name": s["name"],
            "type": s["type"],
            "config": redacted_config,
            "has_secret": redacted_config != config,
            "status": s["status"],
            "last_synced_at": s["last_synced_at"],
            "created_at": s["created_at"],
        })
    return {"sources": result, "count": len(result)}


@app.delete("/sources/{source_id}", summary="删除数据源", description="删除指定数据源")
async def delete_source(source_id: int, user: dict = Depends(require_data_sources_admin)):
    deleted = await asyncio.to_thread(user_db.delete_data_source, source_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return {"status": "deleted"}


def _sync_source(source_id: int) -> dict:
    """同步数据源：根据类型创建对应的 DataSource 实例并 fetch()。

    此函数在 asyncio.to_thread 中调用，不能直接使用 await。
    """
    source = user_db.get_data_source(source_id)
    if source is None:
        raise ValueError("数据源不存在")

    source_type = source["type"]
    config = _json.loads(source["config"]) if source["config"] else {}

    if source_type == "rss":
        from rag.data_sources.rss_source import RSSSource
        ds = RSSSource(url=config.get("url", ""))
    elif source_type == "database":
        from rag.data_sources.db_source import DBSource
        ds = DBSource(
            connection_string=config.get("connection_string", ""),
            query=config.get("query", ""),
        )
    elif source_type == "api":
        from rag.data_sources.api_source import APISource
        ds = APISource(
            url=config.get("url", ""),
            headers=config.get("headers", {}),
            items_path=config.get("items_path"),
            title_field=config.get("title_field", "title"),
            content_field=config.get("content_field", "content"),
            url_field=config.get("url_field", "url"),
            published_at_field=config.get("published_at_field", "published_at"),
        )
    else:
        raise ValueError(f"不支持的数据源类型: {source_type}")

    items = asyncio.run(ds.fetch())

    # Index fetched items into the vector store
    from rag.chunker import chunk as chunk_text
    from rag.embedder import embed as embed_texts
    from rag.vector_store import add as vector_add

    errors = []
    all_chunks = []
    for item in items:
        title = item.get("title", "")
        content = item.get("content", "")
        text = f"{title}\n{content}" if title else content
        if not text.strip():
            continue
        doc_name = title or f"source_{source_id}"
        try:
            chunks = chunk_text(text, doc_name=doc_name)
            all_chunks.extend(chunks)
        except Exception as e:
            errors.append(str(e))

    if all_chunks:
        embeddings = embed_texts([c.text for c in all_chunks])
        vector_add(all_chunks, embeddings)

    # Persist synced status
    user_db.update_data_source_synced(source_id)
    user_db.update_data_source_status(source_id, "active")

    return {"synced": len(items), "indexed": len(all_chunks), "errors": errors}


@app.post("/sources/{source_id}/sync", summary="同步数据源", description="手动触发数据源同步")
async def sync_source(source_id: int, user: dict = Depends(require_data_sources_admin)):
    # Check existence first
    source = await asyncio.to_thread(user_db.get_data_source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    try:
        result = await asyncio.to_thread(_sync_source, source_id)
    except ValueError as exc:
        # 只读 SQL 白名单等业务校验失败 → 400，而不是 500
        raise HTTPException(status_code=400, detail=str(exc))
    return result


# ── 静态文件 & 前端 ────────────────────────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DATA_DIR_PATH = Path(__file__).resolve().parent.parent / "data"


@app.get("/", summary="前端页面", description="返回知识库助手 Web 前端")
def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return JSONResponse(status_code=404, content={"error": "前端文件不存在"})


# Vue Router catch-all: serve index.html for client-side routes
# Only for paths that don't conflict with API endpoints
@app.get("/chat/{path:path}", include_in_schema=False)
@app.get("/login", include_in_schema=False)
def serve_spa():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return JSONResponse(status_code=404, content={"error": "前端文件不存在"})


# Serve only safe subdirectories (NOT the entire data/ which contains .db, jwt_secret, etc.)
DATA_CHARTS_DIR = DATA_DIR_PATH / "charts"
DATA_CHARTS_DIR.mkdir(exist_ok=True)
if DATA_CHARTS_DIR.is_dir():
    app.mount("/data/charts", StaticFiles(directory=str(DATA_CHARTS_DIR)), name="data-charts")
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
