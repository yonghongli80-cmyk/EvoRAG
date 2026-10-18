"""Central application context, shared singletons, and lifecycle management.

Decouples lower-level modules (routers, repositories, pipelines, workers)
from the top-level presentation layer (rag/api.py), eliminating inverted dependencies
and circular lazy imports.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import settings
from rag.concurrency import ReadWriteLock
from rag.user_db import UserDB

if TYPE_CHECKING:
    from fastapi import Request

    from rag.pipeline import RAGPipeline

logger = logging.getLogger(__name__)

# Base storage paths
DATA_DIR: Path = Path(__file__).resolve().parent.parent / "data" / "upload"
KB_FILES_ROOT: Path = Path(__file__).resolve().parent.parent / "data" / "kb_files"
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".txt",
    ".md",
    ".pdf",
    ".docx",
    ".xlsx",
    ".csv",
})
MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10 MB

# Module-level singletons
_DB_PATH = Path(settings.users_db_path)
user_db: UserDB = UserDB(str(_DB_PATH))

pipeline: RAGPipeline | None = None
_pipelines: dict[str, RAGPipeline] = {}
_pipeline_lock: ReadWriteLock = ReadWriteLock()

# Processors & workers
_feedback_processor: Any = None
_gap_analyzer: Any = None
_failure_recorder: Any = None
_processor_lock: threading.Lock = threading.Lock()


def get_user_db(request: Request | None = None) -> UserDB:
    """Return the active UserDB singleton, honoring request/app state if provided."""
    if (
        request is not None
        and hasattr(getattr(request, "app", None), "state")
        and getattr(request.app.state, "user_db", None) is not None
    ):
        return request.app.state.user_db
    return user_db


def set_user_db(new_db: UserDB) -> None:
    """Update active UserDB singleton."""
    global user_db
    user_db = new_db


def get_upload_dir(request: Request | None = None) -> Path:
    """Return the active upload directory, honoring request/app state if provided."""
    if (
        request is not None
        and hasattr(getattr(request, "app", None), "state")
        and getattr(request.app.state, "upload_dir", None) is not None
    ):
        return Path(request.app.state.upload_dir)
    return DATA_DIR


def get_pipeline() -> RAGPipeline | None:
    """Return the active pipeline instance."""
    return pipeline


def set_pipeline(new_pipeline: RAGPipeline | None) -> None:
    """Update the active pipeline."""
    global pipeline
    pipeline = new_pipeline


def get_pipelines() -> dict[str, RAGPipeline]:
    """Return the KB pipelines map."""
    return _pipelines


def get_pipeline_lock() -> ReadWriteLock:
    """Return the pipeline read-write lock."""
    return _pipeline_lock


def get_feedback_processor():
    """返回模块级 FeedbackProcessor 单例，避免每次查询都创建新实例。"""
    global _feedback_processor
    with _processor_lock:
        if _feedback_processor is None:
            from rag.feedback_processor import FeedbackProcessor

            _feedback_processor = FeedbackProcessor(settings.memory_db_path)
    return _feedback_processor


def get_gap_analyzer():
    """返回模块级 GapAnalyzer 单例，避免每次查询都创建新实例。"""
    global _gap_analyzer
    with _processor_lock:
        if _gap_analyzer is None:
            from rag.gap_analyzer import GapAnalyzer

            _gap_analyzer = GapAnalyzer(settings.memory_db_path)
    return _gap_analyzer


def get_failure_recorder():
    """返回模块级 FailureRecorder 单例；观测阶段关闭时不创建任何存储。"""
    global _failure_recorder
    if not settings.evolution_enabled:
        return None
    with _processor_lock:
        if _failure_recorder is None:
            from rag.evolution.recorder import FailureRecorder
            from rag.evolution.store import FailureTraceStore

            _failure_recorder = FailureRecorder(
                FailureTraceStore(settings.evolution_db_path),
                max_queue=settings.evolution_queue_limit,
            )
    return _failure_recorder


def get_question_set_rollout():
    """返回灰度控制器单例；候选功能关闭或未配置放量上限时不启用在线分流。"""
    if not settings.evolution_candidate_enabled:
        return None
    if settings.evolution_candidate_canary_percent <= 0:
        return None
    from rag.evolution.rollout import rollout as controller

    return controller


def _make_pipeline(**kwargs) -> RAGPipeline:
    """构造使用 API 共享数据库连接的 pipeline。"""
    from rag.pipeline import RAGPipeline

    kwargs.setdefault("user_db", get_user_db())
    if "feedback_processor" not in kwargs:
        kwargs["feedback_processor"] = get_feedback_processor()
    if "gap_analyzer" not in kwargs:
        kwargs["gap_analyzer"] = get_gap_analyzer()
    if "failure_recorder" not in kwargs:
        recorder = get_failure_recorder()
        if recorder is not None:
            kwargs["failure_recorder"] = recorder
    if (
        "question_set_rollout" not in kwargs
        and settings.evolution_candidate_enabled
    ):
        kwargs["question_set_rollout"] = get_question_set_rollout()
    return RAGPipeline(**kwargs)


_pipeline_factory = _make_pipeline


def set_pipeline_factory(factory) -> None:
    global _pipeline_factory
    _pipeline_factory = factory


def get_pipeline_factory():
    return _pipeline_factory


def _get_or_create_pipeline(kb_id: str) -> RAGPipeline:
    """Return the cached pipeline for a knowledge base (caller holds write lock)."""
    pipelines = get_pipelines()
    cached = pipelines.get(kb_id)
    cur_pipeline = get_pipeline()
    if cached is None and kb_id == "rag_docs" and cur_pipeline is not None:
        cached = cur_pipeline
        pipelines[kb_id] = cached
    if cached is None:
        cached = _pipeline_factory(kb_id=kb_id)
        pipelines[kb_id] = cached
    if kb_id == "rag_docs":
        set_pipeline(cached)
    return cached


def _get_repo_file_names(data_dir: Path | None = None) -> set[str]:
    """读取 .repo_files manifest，返回仓库文件名集合。"""
    if data_dir is None:
        data_dir = get_upload_dir()
    manifest = data_dir / ".repo_files"
    if not manifest.is_file():
        return set()
    names = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name:
            names.add(name)
    return names


def reset_context() -> None:
    """Reset all cached processors and pipelines."""
    global pipeline, _feedback_processor, _gap_analyzer, _failure_recorder
    lock = get_pipeline_lock()
    pipelines = get_pipelines()
    with lock.write():
        cached_pipelines = list(dict.fromkeys(pipelines.values()))
        pipelines.clear()
        set_pipeline(None)
    for cached_pipeline in cached_pipelines:
        close = getattr(cached_pipeline, "close", None)
        if callable(close):
            close()
    with _processor_lock:
        for processor in (_feedback_processor, _gap_analyzer, _failure_recorder):
            if processor is not None:
                processor.close()
        _feedback_processor = None
        _gap_analyzer = None
        _failure_recorder = None
    _pipeline_factory = _make_pipeline
