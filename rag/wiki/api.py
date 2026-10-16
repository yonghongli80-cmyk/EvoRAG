"""Hypertext Wiki REST API routes for page management and revision control."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from rag.api_security import get_user_db, require_kb_edit, require_kb_view, require_user
from rag.wiki.indexer import WikiIndexer
from rag.wiki.store import WikiStore

router = APIRouter(prefix="/wiki", tags=["wiki"])

_wiki_store: WikiStore | None = None
_wiki_indexer: WikiIndexer | None = None


def get_wiki_store() -> WikiStore:
    global _wiki_store
    if _wiki_store is not None:
        return _wiki_store
    from rag.wiki.store import get_wiki_store as _default_get_store

    return _default_get_store()


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
    request: Request,
    kb_id: str | None = Query(None),
    tag: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
) -> list[dict[str, Any]]:
    db = get_user_db(request)
    if kb_id:
        require_kb_view(db, kb_id, user)

    store = get_wiki_store()
    pages = store.list_pages(kb_id=kb_id, tag=tag, limit=limit, offset=offset)

    if not kb_id and not user.get("is_admin"):
        filtered = []
        kb_perm_cache: dict[str, bool] = {}
        for p in pages:
            if p.kb_id not in kb_perm_cache:
                try:
                    require_kb_view(db, p.kb_id, user)
                    kb_perm_cache[p.kb_id] = True
                except HTTPException:
                    kb_perm_cache[p.kb_id] = False
            if kb_perm_cache[p.kb_id]:
                filtered.append(p)
        pages = filtered

    return [p.model_dump() for p in pages]


@router.post("/pages", status_code=status.HTTP_201_CREATED, summary="创建 Wiki 页面并同步索引")
def create_page(
    request: Request,
    body: CreateWikiPageRequest,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    db = get_user_db(request)
    require_kb_edit(db, body.kb_id, user)

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
    request: Request,
    page_id: int,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_view(db, page.kb_id, user)

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
    request: Request,
    page_id: int,
    body: UpdateWikiPageRequest,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    indexer = get_wiki_indexer()

    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_edit(db, page.kb_id, user)
    old_title = page.title

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

    chunks_indexed = indexer.sync_page(page_id, old_title=old_title)
    return {
        "page": updated.model_dump(),
        "chunks_indexed": chunks_indexed,
    }


@router.delete("/pages/{page_id}", summary="删除 Wiki 页面并清除索引")
def delete_page(
    request: Request,
    page_id: int,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    indexer = get_wiki_indexer()

    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_edit(db, page.kb_id, user)

    indexer.delete_page(page_id, kb_id=page.kb_id, title=page.title)
    deleted = store.delete_page(page_id)
    return {"deleted": deleted, "id": page_id}


@router.get("/pages/{page_id}/history", summary="查看页面修订历史")
def get_page_history(
    request: Request,
    page_id: int,
    user: dict = Depends(require_user),
) -> list[dict[str, Any]]:
    store = get_wiki_store()
    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_view(db, page.kb_id, user)

    revisions = store.get_revisions(page_id)
    return [r.model_dump() for r in revisions]


@router.post("/pages/{page_id}/rollback", summary="秒级回滚页面至指定历史版本")
def rollback_page(
    request: Request,
    page_id: int,
    body: RollbackWikiPageRequest,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    indexer = get_wiki_indexer()

    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_edit(db, page.kb_id, user)
    old_title = page.title

    try:
        updated = store.rollback_revision(
            page_id=page_id,
            revision_num=body.revision_num,
            author_id=user.get("username"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    chunks_indexed = indexer.sync_page(page_id, old_title=old_title)
    return {
        "page": updated.model_dump(),
        "chunks_indexed": chunks_indexed,
    }


@router.get("/pages/{page_id}/revisions/{revision_num}/diff", summary="获取指定版本的 diff 详情")
def get_revision_diff(
    request: Request,
    page_id: int,
    revision_num: int,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()
    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_view(db, page.kb_id, user)

    rev = store.get_revision_by_num(page_id, revision_num)
    if not rev:
        raise HTTPException(status_code=404, detail=f"版本 #{revision_num} 不存在")

    return {
        "page_id": page_id,
        "revision_num": revision_num,
        "delta": rev.delta,
        "comment": rev.comment,
        "sha256_hash": rev.sha256_hash,
        "created_at": rev.created_at,
    }

