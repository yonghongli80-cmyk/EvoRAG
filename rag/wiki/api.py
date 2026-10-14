"""Hypertext Wiki REST API routes for page management and revision control."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from rag.api_security import require_user
from rag.wiki.indexer import WikiIndexer
from rag.wiki.store import WikiStore

router = APIRouter(prefix="/wiki", tags=["wiki"])

_wiki_store: WikiStore | None = None
_wiki_indexer: WikiIndexer | None = None


def get_wiki_store() -> WikiStore:
    global _wiki_store
    if _wiki_store is None:
        _wiki_store = WikiStore()
    return _wiki_store


def get_wiki_indexer() -> WikiIndexer:
    global _wiki_indexer
    if _wiki_indexer is None:
        _wiki_indexer = WikiIndexer(wiki_store=get_wiki_store())
    return _wiki_indexer


class CreateWikiPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: str
    title: str
    content: str
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)


class UpdateWikiPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    comment: str = "Update"


class RollbackWikiPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_num: int


@router.get("/pages", summary="列出 Wiki 页面")
def list_pages(
    kb_id: str | None = Query(None),
    tag: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
) -> list[dict[str, Any]]:
    store = get_wiki_store()
    pages = store.list_pages(kb_id=kb_id, tag=tag, limit=limit, offset=offset)
    return [p.model_dump() for p in pages]


@router.post("/pages", status_code=status.HTTP_201_CREATED, summary="创建 Wiki 页面并同步索引")
def create_page(
    body: CreateWikiPageRequest,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    indexer = get_wiki_indexer()

    try:
        page = store.create_page(
            kb_id=body.kb_id,
            title=body.title,
            content=body.content,
            slug=body.slug,
            tags=body.tags,
            author_id=user.get("username"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    chunks_indexed = indexer.sync_page(page.id)
    return {
        "page": page.model_dump(),
        "chunks_indexed": chunks_indexed,
    }


@router.get("/pages/{page_id}", summary="获取 Wiki 页面详情及双向链接")
def get_page(
    page_id: int,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    outgoing = store.get_outgoing_links(page_id)
    backlinks = [b.model_dump() for b in store.get_backlinks(page_id)]
    revisions = store.get_revisions(page_id)

    return {
        "page": page.model_dump(),
        "outgoing_links": outgoing,
        "backlinks": backlinks,
        "revision_count": len(revisions),
    }


@router.put("/pages/{page_id}", summary="修改 Wiki 页面并生成增量修订版")
def update_page(
    page_id: int,
    body: UpdateWikiPageRequest,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    indexer = get_wiki_indexer()

    try:
        updated = store.update_page(
            page_id=page_id,
            title=body.title,
            content=body.content,
            tags=body.tags,
            comment=body.comment,
            author_id=user.get("username"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    chunks_indexed = indexer.sync_page(page_id)
    return {
        "page": updated.model_dump(),
        "chunks_indexed": chunks_indexed,
    }


@router.delete("/pages/{page_id}", summary="删除 Wiki 页面并清除索引")
def delete_page(
    page_id: int,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    indexer = get_wiki_indexer()

    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    indexer.delete_page(page_id, kb_id=page.kb_id, title=page.title)
    deleted = store.delete_page(page_id)
    return {"deleted": deleted, "id": page_id}


@router.get("/pages/{page_id}/history", summary="查看页面修订历史")
def get_page_history(
    page_id: int,
    user: dict = Depends(require_user),
) -> list[dict[str, Any]]:
    store = get_wiki_store()
    revisions = store.get_revisions(page_id)
    return [r.model_dump() for r in revisions]


@router.post("/pages/{page_id}/rollback", summary="秒级回滚页面至指定历史版本")
def rollback_page(
    page_id: int,
    body: RollbackWikiPageRequest,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    indexer = get_wiki_indexer()

    try:
        page = store.rollback_revision(
            page_id=page_id,
            revision_num=body.revision_num,
            author_id=user.get("username"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    chunks_indexed = indexer.sync_page(page_id)
    return {
        "page": page.model_dump(),
        "chunks_indexed": chunks_indexed,
    }
