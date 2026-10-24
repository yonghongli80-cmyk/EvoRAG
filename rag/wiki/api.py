"""Hypertext Wiki REST API routes for page management and revision control."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from rag.api_security import get_user_db, require_kb_edit, require_kb_view, require_user
from rag.kb_lifecycle import KBLifecycleError, kb_mutation
from rag.kb_lifecycle_http import lifecycle_http_error
from rag.user_db import UserDB
from rag.wiki.indexer import WikiIndexer, enqueue_wiki_reindex_job
from rag.wiki.models import WikiPage
from rag.wiki.store import WikiStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wiki", tags=["wiki"])

#: Upper bound for page markdown. Without one a single request can persist
#: arbitrarily large content and hand it to the chunker/embedder.
WIKI_CONTENT_MAX_CHARS = 200_000

#: 只缓存 indexer。store 的单例由 ``rag.wiki.store.get_wiki_store`` 持有（``reset_wiki_store`` 也管它），
#: 这里曾经另有一个 ``_wiki_store`` 全局，但**从未被赋值**：它看起来像第二层缓存，实际是死代码。
_wiki_indexer: WikiIndexer | None = None


def get_wiki_store() -> WikiStore:
    from rag.wiki.store import get_wiki_store as _default_get_store

    return _default_get_store()


def get_wiki_indexer(user_db: UserDB | None = None) -> WikiIndexer:
    global _wiki_indexer
    current_store = get_wiki_store()
    if (
        _wiki_indexer is None
        or _wiki_indexer.store is not current_store
        or _wiki_indexer._user_db is not user_db
    ):
        _wiki_indexer = WikiIndexer(wiki_store=current_store, user_db=user_db)
    return _wiki_indexer


class CreateWikiPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: str
    title: str
    content: str = Field(max_length=WIKI_CONTENT_MAX_CHARS)
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)


class UpdateWikiPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    content: str | None = Field(default=None, max_length=WIKI_CONTENT_MAX_CHARS)
    tags: list[str] | None = None
    comment: str = "Update"


class RollbackWikiPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_num: int


def _sync_page_index(
    db: Any,
    indexer: WikiIndexer,
    page: WikiPage,
    *,
    old_title: str | None = None,
) -> dict[str, Any]:
    """Index a page without turning a vector-store failure into a lost page.

    The page content is already committed by the store, so a failed index write
    must not fail the request: ``WikiIndexer.sync_page`` writes the new revision
    before deleting the old one (the previous revision stays searchable) and
    this helper records a ``wiki_reindex`` job for the shared worker to retry.
    """
    try:
        chunks_indexed = indexer.sync_page(page.id, old_title=old_title)
    except Exception:
        logger.exception("wiki page %s index sync failed; queueing a retry", page.id)
        queued = enqueue_wiki_reindex_job(
            db, page.id, page.current_revision_id, kb_id=page.kb_id
        )
        return {
            "chunks_indexed": 0,
            "index_status": "retry_queued" if queued else "failed",
        }
    return {"chunks_indexed": chunks_indexed, "index_status": "ok"}


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
    indexer = get_wiki_indexer(db)

    try:
        with kb_mutation(db, body.kb_id):
            page = store.create_page(
                kb_id=body.kb_id,
                title=body.title,
                content=body.content,
                slug=body.slug,
                tags=body.tags,
                author_id=user.get("username"),
            )
            sync_result = _sync_page_index(db, indexer, page)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc

    return {"page": page.model_dump(), **sync_result}


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

    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_edit(db, page.kb_id, user)
    indexer = get_wiki_indexer(db)

    try:
        with kb_mutation(db, page.kb_id):
            page = store.get_page(page_id)
            if not page:
                raise HTTPException(status_code=404, detail="Wiki 页面不存在")
            old_title = page.title
            updated = store.update_page(
                page_id=page_id,
                title=body.title,
                content=body.content,
                tags=body.tags,
                comment=body.comment,
                author_id=user.get("username"),
            )
            sync_result = _sync_page_index(db, indexer, updated, old_title=old_title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc

    return {"page": updated.model_dump(), **sync_result}


@router.delete("/pages/{page_id}", summary="删除 Wiki 页面并清除索引")
def delete_page(
    request: Request,
    page_id: int,
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    store = get_wiki_store()

    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_edit(db, page.kb_id, user)
    indexer = get_wiki_indexer(db)

    try:
        with kb_mutation(db, page.kb_id):
            deleted = indexer.delete_page_and_index(page_id)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
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

    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    db = get_user_db(request)
    require_kb_edit(db, page.kb_id, user)
    indexer = get_wiki_indexer(db)

    try:
        with kb_mutation(db, page.kb_id):
            page = store.get_page(page_id)
            if not page:
                raise HTTPException(status_code=404, detail="Wiki 页面不存在")
            old_title = page.title
            updated = store.rollback_revision(
                page_id=page_id,
                revision_num=body.revision_num,
                author_id=user.get("username"),
            )
            sync_result = _sync_page_index(db, indexer, updated, old_title=old_title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc

    return {"page": updated.model_dump(), **sync_result}


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

