"""RAG Knowledge Base FastAPI application assembly and lifecycle."""

from __future__ import annotations

import logging
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import rag.api_startup as api_startup
from config import settings
from rag.api_security import log_security_mode as log_security_mode
from rag.backups.scheduler import start_backup_scheduler, stop_backup_scheduler
from rag.context import (
    _DB_PATH as _DB_PATH,
)
from rag.context import (
    DATA_DIR as DATA_DIR,
)
from rag.context import (
    KB_FILES_ROOT as KB_FILES_ROOT,
)
from rag.context import (
    SUPPORTED_EXTENSIONS as SUPPORTED_EXTENSIONS,
)
from rag.context import (
    _get_or_create_pipeline,
    _pipeline_lock,
    get_pipeline,
    get_user_db,
    reset_context,
    set_pipeline,
    set_user_db,
    user_db,
)
from rag.context import (
    _make_pipeline as _make_pipeline,
)
from rag.context import (
    get_failure_recorder as get_failure_recorder,
)
from rag.context import (
    get_feedback_processor as get_feedback_processor,
)
from rag.context import (
    get_gap_analyzer as get_gap_analyzer,
)
from rag.document_sources.upload import UploadDocumentSource as UploadDocumentSource
from rag.document_versions import ingest_document_version as ingest_document_version
from rag.evolution.api import router as evolution_router
from rag.evolution.worker import start_evolution_worker, stop_evolution_worker
from rag.kb_metadata import generate_summary as generate_summary
from rag.kb_metadata import generate_toc as generate_toc
from rag.knowledge_base import KnowledgeBaseManager as KnowledgeBaseManager
from rag.llm_pool import get_llm_pool_router as get_llm_pool_router
from rag.loader import load as load_document
from rag.logging_config import logging_context, normalize_request_id, setup_logging
from rag.login_throttle import LoginThrottle
from rag.materials.api import router as materials_router
from rag.materials.supervisor import MaterialWorkerSupervisor
from rag.operations_api import router as operations_router
from rag.org_auth.api import router as organization_router
from rag.reliability.api import answers_router
from rag.reliability.api import router as reliability_router
from rag.routers import (
    analysis_router,
    auth_router,
    documents_router,
    knowledge_router,
    query_router,
    sources_router,
    spreadsheets_router,
    system_router,
)
from rag.routers.analysis import (
    _generate_summary_llm as _generate_summary_llm,
)
from rag.routers.analysis import (
    _suggest_card_llm as _suggest_card_llm,
)
from rag.routers.documents import _check_files_in_kb as _check_files_in_kb
from rag.routers.query import _submit_feedback_observation as _submit_feedback_observation
from rag.routers.system import (
    get_gap_summary as get_gap_summary,
)
from rag.routers.system import (
    get_gaps as get_gaps,
)
from rag.routers.system import (
    record_http_request as record_http_request,
)
from rag.startup_lifecycle import StartupLifecycle
from rag.wiki.api import router as wiki_router

# Module-level logging
setup_logging()
logger = logging.getLogger(__name__)

_SESSION_COOKIE = "rag_session"
_login_throttle = LoginThrottle(
    settings.login_failure_limit,
    settings.login_failure_window_seconds,
)


def restore_question_set_rollout():
    """Rebuild 灰度控制器状态：重启时仍在灰度的候选按审计轨迹恢复百分比。"""
    if not settings.evolution_candidate_enabled:
        return
    from rag.evolution.rollout import rollout as controller

    if settings.evolution_db_path and Path(settings.evolution_db_path).exists():
        from rag.evolution.candidate_store import CandidateStore
        from rag.evolution.store import FailureTraceStore

        trace_store = FailureTraceStore(settings.evolution_db_path)
        try:
            controller.restore(CandidateStore(trace_store))
        finally:
            trace_store.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup_lifecycle = _app.state.startup_lifecycle_owner
    startup_lifecycle.enter()
    primary_error = None
    try:
        _run_legacy_kb_migration()
        await startup_lifecycle.to_thread(auto_index_on_startup)
        await startup_lifecycle.to_thread(_run_staged_version_migration)
        start_backup_scheduler()
        start_evolution_worker()
        restore_question_set_rollout()
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_hooks = (
            ("stop_backup_scheduler", stop_backup_scheduler),
            ("stop_evolution_worker", stop_evolution_worker),
            ("close_pipeline_processors", close_pipeline_processors),
        )
        await startup_lifecycle.finish(cleanup_hooks, primary_error)


app = FastAPI(
    title="RAG 知识库 API",
    description="基于检索增强生成的智能知识库系统",
    lifespan=lifespan,
)
app.state.startup_lifecycle_owner = StartupLifecycle()

# Mount modular routers
app.include_router(operations_router)
app.include_router(evolution_router)
app.include_router(materials_router)
app.include_router(spreadsheets_router)
app.include_router(reliability_router)
app.include_router(answers_router)
app.include_router(organization_router)
app.include_router(wiki_router)
app.include_router(analysis_router)
app.include_router(system_router)
app.include_router(auth_router)
app.include_router(query_router)
app.include_router(documents_router)
app.include_router(knowledge_router)
app.include_router(sources_router)


def _versions_root() -> Path:
    """Resolve the configured document-versions root from app state."""
    return Path(getattr(app.state, "document_versions_root", settings.document_versions_root))


_material_worker_supervisor = MaterialWorkerSupervisor(user_db, _versions_root)


def _start_material_worker() -> None:
    """Compatibility alias; ownership lives in ``MaterialWorkerSupervisor``."""
    _material_worker_supervisor.start()


def _stop_material_worker() -> bool:
    """Compatibility alias; ownership lives in ``MaterialWorkerSupervisor``."""
    return _material_worker_supervisor.stop()


def _run_document_version_migration() -> None:
    """Compatibility facade for the startup migration implementation."""
    return api_startup._run_document_version_migration(sys.modules[__name__])


def _run_material_metadata_migration() -> None:
    """Compatibility facade for the startup migration implementation."""
    return api_startup._run_material_metadata_migration(sys.modules[__name__])


def _run_spreadsheet_migration() -> None:
    """Compatibility facade for the startup migration implementation."""
    return api_startup._run_spreadsheet_migration(sys.modules[__name__])


def _register_repo_document_version(doc_name: str, source_path: Path) -> bool:
    """Compatibility facade for repository document version registration."""
    return api_startup._register_repo_document_version(sys.modules[__name__], doc_name, source_path)


def _run_legacy_kb_migration() -> None:
    """Compatibility facade for the startup migration implementation."""
    return api_startup._run_legacy_kb_migration(sys.modules[__name__])


def _run_staged_version_migration() -> None:
    """Compatibility facade for staged document version repair."""
    return api_startup._run_staged_version_migration(sys.modules[__name__])


def auto_index_on_startup():
    """启动时增量索引 data/upload/ 下的文件。"""
    return api_startup.auto_index_on_startup(sys.modules[__name__])


@app.middleware("http")
async def browser_session_middleware(request, call_next):
    """Translate the HttpOnly browser cookie into the existing auth header path."""
    header_names = {key.lower() for key, _ in request.scope["headers"]}
    token = request.cookies.get(_SESSION_COOKIE)
    if token and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        expected_origin = f"{scheme}://{request.headers.get('host', '')}"
        fetch_site = request.headers.get("sec-fetch-site", "")
        if (origin and origin.rstrip("/") != expected_origin.rstrip("/")) or fetch_site == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "跨站请求被拒绝"})
    if token and b"authorization" not in header_names:
        request.scope["headers"] = [
            *request.scope["headers"],
            (b"authorization", f"Bearer {token}".encode("latin-1")),
        ]
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    request_id = normalize_request_id(request.headers.get("X-Request-ID"))
    with logging_context(request_id=request_id):
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# Vue Router SPA fallback
_VUE_ROUTES = {
    "/files",
    "/kb",
    "/analysis",
    "/analytics",
    "/operations",
    "/organization",
    "/settings/password",
    "/login",
    "/wiki",
    "/mode/file",
    "/mode/kb",
    "/mode/analysis",
}
_VUE_ROUTE_PREFIXES = {"/kb/", "/operations/"}
_VUE_ROUTE_PATTERNS = (
    re.compile(r"^/documents/\d+/versions/\d+/spreadsheet$"),
    re.compile(r"^/wiki/\d+$"),
)


@app.middleware("http")
async def spa_fallback_middleware(request, call_next):
    path = request.url.path
    accept = request.headers.get("accept", "")
    has_auth = "authorization" in {k.lower() for k in request.headers}
    if request.method in {"GET", "HEAD"} and "text/html" in accept and not has_auth:
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


@app.middleware("http")
async def metrics_middleware(request, call_next):
    """Record route-level request counters without storing raw paths or payloads.

    The scrape endpoint itself is excluded so polling does not distort its own
    numbers. Route templates (``/files/{filename}/download``) keep label
    cardinality bounded and avoid leaking document names into metrics.
    """
    if request.url.path == "/metrics":
        return await call_next(request)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "") or "unmatched"
        record_http_request(request.method, route_path, status_code, time.perf_counter() - started)


app.state.user_db = user_db
app.state.document_versions_root = settings.document_versions_root
app.state.structured_datasets_root = settings.structured_datasets_root
app.state.kb_files_root = str(KB_FILES_ROOT)


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
    if not _stop_material_worker():
        return
    reset_context()
    from rag.evolution.api import close_store
    from rag.vector_store import close_client

    close_store()
    close_client()


# ── 静态文件 & 前端 ────────────────────────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DATA_DIR_PATH = Path(__file__).resolve().parent.parent / "data"


@app.get("/", summary="前端页面", description="返回知识库助手 Web 前端")
def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return JSONResponse(status_code=404, content={"error": "前端文件不存在"})


@app.get("/chat/{path:path}", include_in_schema=False)
@app.get("/login", include_in_schema=False)
def serve_spa():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return JSONResponse(status_code=404, content={"error": "前端文件不存在"})


DATA_CHARTS_DIR = DATA_DIR_PATH / "charts"
DATA_CHARTS_DIR.mkdir(exist_ok=True)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Backward compatibility aliases for testing
_feedback_processor = None
_gap_analyzer = None
_failure_recorder = None
load_document = load_document
_submit_feedback_observation = _submit_feedback_observation


class _ApiModule(sys.modules[__name__].__class__):
    """Module-level attribute forwarding, with the local-binding half that was missing.

    这个装配器把 `user_db` / `DATA_DIR` / `pipeline` 等单例转发到 `rag.context`，让路由层不必反向
    依赖本模块。但 `rag/api.py` **自己的函数**读的是本模块的裸全局（`LOAD_GLOBAL` 查模块 `__dict__`），
    而 forwarding 只写在属性 setter 里 —— 于是 `monkeypatch.setattr(rag.api, "user_db", tmp_db)`
    会"看起来成功"（属性读回来是新对象），本模块内部却仍用旧对象。

    实测后果（2026-10-18）：启动恢复/素材迁移的用例把临时库 patch 进来，`_run_material_metadata_migration()`
    却在**真实仓库库**上建表并回填。所以每个 setter 都要同时写回本模块 `__dict__`。
    """

    def _bind_local(self, name, value):
        sys.modules[__name__].__dict__[name] = value

    @property
    def pipeline(self):
        return get_pipeline()

    @pipeline.setter
    def pipeline(self, value):
        set_pipeline(value)
        self._bind_local("pipeline", value)

    @property
    def _pipelines(self):
        import rag.context as _ctx

        return _ctx.get_pipelines()

    @_pipelines.setter
    def _pipelines(self, value):
        import rag.context as _ctx

        _ctx._pipelines = value
        self._bind_local("_pipelines", value)

    @property
    def user_db(self):
        return get_user_db()

    @user_db.setter
    def user_db(self, value):
        set_user_db(value)
        if hasattr(self, "app") and hasattr(self.app, "state"):
            self.app.state.user_db = value
        self._bind_local("user_db", value)

    @property
    def _feedback_processor(self):
        import rag.context as _ctx

        return _ctx._feedback_processor

    @_feedback_processor.setter
    def _feedback_processor(self, value):
        import rag.context as _ctx

        _ctx._feedback_processor = value
        self._bind_local("_feedback_processor", value)

    @property
    def _gap_analyzer(self):
        import rag.context as _ctx

        return _ctx._gap_analyzer

    @_gap_analyzer.setter
    def _gap_analyzer(self, value):
        import rag.context as _ctx

        _ctx._gap_analyzer = value
        self._bind_local("_gap_analyzer", value)

    @property
    def _failure_recorder(self):
        import rag.context as _ctx

        return _ctx._failure_recorder

    @_failure_recorder.setter
    def _failure_recorder(self, value):
        import rag.context as _ctx

        _ctx._failure_recorder = value
        self._bind_local("_failure_recorder", value)

    @property
    def _DB_PATH(self):
        import rag.context as _ctx

        return _ctx._DB_PATH

    @_DB_PATH.setter
    def _DB_PATH(self, value):
        import rag.context as _ctx

        _ctx._DB_PATH = value
        self._bind_local("_DB_PATH", value)

    @property
    def DATA_DIR(self):
        import rag.context as _ctx

        return _ctx.DATA_DIR

    @DATA_DIR.setter
    def DATA_DIR(self, value):
        import rag.context as _ctx
        import rag.routers.documents as _doc_mod

        _ctx.DATA_DIR = value
        _doc_mod.DATA_DIR = value
        self._bind_local("DATA_DIR", value)

    @property
    def KB_FILES_ROOT(self):
        import rag.context as _ctx

        return _ctx.KB_FILES_ROOT

    @KB_FILES_ROOT.setter
    def KB_FILES_ROOT(self, value):
        import rag.context as _ctx
        import rag.routers.knowledge as _kb_mod

        _ctx.KB_FILES_ROOT = value
        _kb_mod.KB_FILES_ROOT = value
        self._bind_local("KB_FILES_ROOT", value)

    @property
    def _make_pipeline(self):
        import rag.context as _ctx

        return _ctx.get_pipeline_factory()

    @_make_pipeline.setter
    def _make_pipeline(self, value):
        import rag.context as _ctx

        _ctx.set_pipeline_factory(value)
        self._bind_local("_make_pipeline", value)

    def __setattr__(self, name, value):
        if name == "KnowledgeBaseManager":
            import rag.routers.documents as _doc_mod
            import rag.routers.knowledge as _kb_mod

            _doc_mod.KnowledgeBaseManager = value
            _kb_mod.KnowledgeBaseManager = value
        elif name in ("get_failure_recorder", "get_gap_analyzer", "get_feedback_processor"):
            import rag.context as _ctx
            import rag.routers.query as _query_mod
            import rag.routers.system as _system_mod

            setattr(_ctx, name, value)
            setattr(_query_mod, name, value)
            setattr(_system_mod, name, value)
        elif name == "_make_pipeline":
            import rag.context as _ctx

            _ctx.set_pipeline_factory(value)
        elif name in ("load_document", "generate_toc", "generate_summary"):
            import rag.kb_metadata as _kb_meta
            import rag.loader as _loader
            import rag.routers.documents as _doc_mod
            import rag.routers.knowledge as _kb_mod

            if name == "load_document":
                _loader.load = value
            elif name in ("generate_toc", "generate_summary"):
                setattr(_kb_meta, name, value)
            setattr(_doc_mod, name, value)
            setattr(_kb_mod, name, value)
        elif name in ("_suggest_card_llm", "_generate_summary_llm"):
            import rag.routers.analysis as _analysis_mod

            setattr(_analysis_mod, name, value)
        elif name == "get_llm_pool_router":
            import rag.llm_pool as _llm_pool_mod
            import rag.routers.query as _query_mod

            setattr(_llm_pool_mod, name, value)
            setattr(_query_mod, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in ("_suggest_card_llm", "_generate_summary_llm"):
            import rag.routers.analysis as _analysis_mod

            default_name = f"_DEFAULT{name.upper()}"
            if hasattr(_analysis_mod, default_name):
                setattr(_analysis_mod, name, getattr(_analysis_mod, default_name))
            elif hasattr(_analysis_mod, name):
                delattr(_analysis_mod, name)
        elif name == "get_llm_pool_router":
            import rag.llm_pool as _llm_pool_mod
            import rag.routers.query as _query_mod

            if hasattr(_llm_pool_mod, name):
                delattr(_llm_pool_mod, name)
            if hasattr(_query_mod, name):
                delattr(_query_mod, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _ApiModule
