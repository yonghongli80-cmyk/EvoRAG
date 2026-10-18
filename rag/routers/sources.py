"""Data source management routes."""

from __future__ import annotations

import asyncio
import json as _json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from rag.api_security import (
    get_user_db,
    require_data_sources_admin,
)
from rag.auth import decode_token
from rag.chunker import chunk as chunk_text
from rag.context import user_db as default_user_db
from rag.data_sources.api_source import APISource
from rag.data_sources.db_source import DBSource
from rag.data_sources.rss_source import RSSSource
from rag.embedder import embed as embed_texts
from rag.repositories.analysis_repo import mask_data_source_config
from rag.vector_store import add as vector_add

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sources"])


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


def _resolve_source_owner(user_id: str, authorization: str = "", db=None) -> str:
    """把 /sources 端点的鉴权信息解析为数据源 owner_id（TEXT）。"""
    if db is None:
        db = default_user_db
    token = (
        authorization.replace("Bearer ", "")
        if authorization.startswith("Bearer ")
        else ""
    )
    if token:
        payload = decode_token(token)
        if payload and "user_id" in payload:
            user = db.get_user_by_id(payload["user_id"])
            if user:
                return str(user["id"])
    if user_id and user_id not in ("", "anonymous"):
        return str(user_id)
    return "anonymous"


def _sync_source(source_id: int, db=None) -> dict:
    """同步数据源：根据类型创建对应的 DataSource 实例并 fetch()。"""
    if db is None:
        db = default_user_db
    source = db.get_data_source(source_id)
    if source is None:
        raise ValueError("数据源不存在")

    source_type = source["type"]
    config = _json.loads(source["config"]) if source["config"] else {}

    if source_type == "rss":
        ds = RSSSource(url=config.get("url", ""))
    elif source_type == "database":
        ds = DBSource(
            connection_string=config.get("connection_string", ""),
            query=config.get("query", ""),
        )
    elif source_type == "api":
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
    db.update_data_source_synced(source_id)
    db.update_data_source_status(source_id, "active")

    return {"synced": len(items), "indexed": len(all_chunks), "errors": errors}


@router.post("/sources", summary="创建数据源", description="创建新的数据源（RSS / 数据库 / API）")
async def create_source(req: CreateSourceRequest, request: Request, user: dict = Depends(require_data_sources_admin)):
    user_db = get_user_db(request)
    source_id = await asyncio.to_thread(
        user_db.create_data_source,
        req.name,
        req.type,
        _json.dumps(req.config, ensure_ascii=False),
        str(user["id"]),
    )
    masked_cfg, has_secret = mask_data_source_config(req.config)
    return {
        "id": source_id,
        "name": req.name,
        "type": req.type,
        "config": masked_cfg,
        "has_secret": has_secret,
        "status": "inactive",
        "last_synced_at": None,
        "created_at": 0.0,
    }


@router.get("/sources", summary="列出数据源", description="获取所有数据源列表")
async def list_sources(
    request: Request,
    user_id: str = Query(default=""),
    user: dict = Depends(require_data_sources_admin),
):
    user_db = get_user_db(request)
    if user.get("is_admin"):
        sources = await asyncio.to_thread(user_db.list_data_sources)
    else:
        owner_id = _resolve_source_owner(user_id, "", user_db)
        sources = await asyncio.to_thread(user_db.list_data_sources, owner_id)
    result = []
    for s in sources:
        raw_cfg = s.get("config") if isinstance(s, dict) else getattr(s, "config", None)
        if isinstance(raw_cfg, dict):
            cfg = raw_cfg
        elif raw_cfg:
            try:
                cfg = _json.loads(raw_cfg)
            except (ValueError, TypeError):
                cfg = {}
        else:
            cfg = {}
        masked_cfg, has_secret = mask_data_source_config(cfg)
        result.append({
            "id": s["id"],
            "name": s["name"],
            "type": s["type"],
            "config": masked_cfg,
            "has_secret": has_secret,
            "status": s["status"],
            "last_synced_at": s["last_synced_at"],
            "created_at": s["created_at"],
        })
    return {"sources": result, "count": len(result)}


@router.delete("/sources/{source_id}", summary="删除数据源", description="删除指定数据源")
async def delete_source(source_id: int, request: Request, user: dict = Depends(require_data_sources_admin)):
    user_db = get_user_db(request)
    deleted = await asyncio.to_thread(user_db.delete_data_source, source_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return {"status": "deleted"}


@router.post("/sources/{source_id}/sync", summary="同步数据源", description="手动触发数据源同步")
async def sync_source(source_id: int, request: Request, user: dict = Depends(require_data_sources_admin)):
    user_db = get_user_db(request)
    source = await asyncio.to_thread(user_db.get_data_source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    try:
        result = await asyncio.to_thread(_sync_source, source_id, db=user_db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result
