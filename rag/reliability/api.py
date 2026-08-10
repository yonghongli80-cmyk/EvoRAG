"""Permission-first API boundary for answer-reliability operations."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request

from rag.api_security import get_user_db, require_user
from rag.permissions import check_doc_permission
from rag.reliability.comparison import ComparisonRequest, ComparisonResult, DocumentEvidenceTool
from rag.reliability.models import ComparisonScope
from rag.reliability.service import execute_persisted_comparison

router = APIRouter(prefix="/reliability", tags=["reliability"])
answers_router = APIRouter(tags=["reliability"])


def build_comparison_scope(user_db, user: dict, document_ids: tuple[int, ...]) -> ComparisonScope:
    """Authorize all selected documents before a comparison tool is invoked."""
    documents = user_db.get_document_permissions_by_ids(list(document_ids))
    if len(documents) != len(document_ids):
        raise HTTPException(status_code=403, detail="无权比较所选资料")
    try:
        for document_id in document_ids:
            document = documents[document_id]
            check_doc_permission(user_db, document["doc_name"], document["kb_id"], user, "view")
    except HTTPException as exc:
        raise HTTPException(status_code=403, detail="无权比较所选资料") from exc

    fingerprint = hashlib.sha256(
        f"{user['id']}:{','.join(str(doc_id) for doc_id in sorted(document_ids))}".encode()
    ).hexdigest()
    return ComparisonScope(
        actor_id=int(user["id"]),
        allowed_document_ids=frozenset(document_ids),
        allowed_document_versions=frozenset(
            (document_id, document["current_version_id"])
            for document_id, document in documents.items()
            if document.get("current_version_id") is not None
        ),
        permission_scope_fingerprint=fingerprint,
    )


@router.post("/compare", response_model=ComparisonResult)
async def compare(
    comparison_request: ComparisonRequest,
    request: Request,
    user: dict = Depends(require_user),  # noqa: B008 - FastAPI dependency declaration
) -> ComparisonResult:
    user_db = get_user_db(request)
    scope = build_comparison_scope(user_db, user, comparison_request.document_ids)
    document_tool: DocumentEvidenceTool | None = getattr(
        request.app.state, "comparison_document_tool", None
    )
    if document_tool is None:
        factory = getattr(request.app.state, "comparison_document_tool_factory", None)
        if callable(factory):
            document_tool = factory()
    if document_tool is None:
        raise HTTPException(status_code=503, detail="文档比较服务暂不可用")
    return await execute_persisted_comparison(
        comparison_request,
        scope,
        document_tool,
        user_db,
        int(user["id"]),
    )


@router.get("/comparisons/{comparison_id}", response_model=ComparisonResult)
async def get_comparison(
    comparison_id: int,
    request: Request,
    user: dict = Depends(require_user),  # noqa: B008 - FastAPI dependency declaration
) -> ComparisonResult:
    user_db = get_user_db(request)
    is_admin = bool(user.get("is_admin"))
    comparison = user_db.get_document_comparison(comparison_id, int(user["id"]), is_admin)
    document_ids = user_db.get_document_comparison_document_ids(
        comparison_id, int(user["id"]), is_admin
    )
    if comparison is None or document_ids is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    documents = user_db.get_document_permissions_by_ids(list(document_ids))
    if len(documents) != len(document_ids):
        raise HTTPException(status_code=404, detail="comparison not found")
    try:
        for document_id in document_ids:
            document = documents[document_id]
            check_doc_permission(user_db, document["doc_name"], document["kb_id"], user, "view")
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail="comparison not found") from exc
    return comparison


@answers_router.get("/answers/{message_id}/reliability")
async def answer_reliability(
    message_id: int,
    request: Request,
    user: dict = Depends(require_user),  # noqa: B008 - FastAPI dependency declaration
) -> dict:
    reliability = get_user_db(request).get_message_reliability(message_id, user["id"])
    if reliability is None:
        raise HTTPException(status_code=404, detail="可靠性记录不存在")
    return reliability
