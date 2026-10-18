"""System and health check routes."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

import rag.vector_store as vector_store
from config import settings
from rag.api_security import require_admin
from rag.context import get_gap_analyzer

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", summary="健康检查", description="检查服务是否正常运行")
def health():
    components = {}
    try:
        qdrant_client = vector_store._get_client()
        qdrant_client.get_collections()
        components["qdrant"] = "ok"
    except Exception as e:
        logger.error("Qdrant 健康检查失败: %s", e)
        components["qdrant"] = "error"

    try:
        conn = sqlite3.connect(settings.analysis_db_path)
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


def _get_gap_analyzer_instance():
    return get_gap_analyzer()


@router.get("/analytics/gaps/summary", summary="检索空白分析统计")
async def get_gap_summary(user: dict = Depends(require_admin)):
    analyzer = _get_gap_analyzer_instance()
    return await asyncio.to_thread(analyzer.get_summary)


@router.get("/analytics/gaps", summary="未解答问题列表")
async def get_gaps(limit: int = 50, user: dict = Depends(require_admin)):
    analyzer = _get_gap_analyzer_instance()
    return await asyncio.to_thread(analyzer.get_gaps, limit=limit)

