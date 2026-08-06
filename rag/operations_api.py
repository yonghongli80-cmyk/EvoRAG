"""Knowledge operations router: versions, feedback, tasks.

Extended by Tasks 5-7. Mounted exactly once by ``rag.api``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from rag.api_security import require_doc_view, require_user

router = APIRouter()


class KnowledgeFeedbackRequest(BaseModel):
    assistant_message_id: int
    kind: str = Field(pattern="^(useful|not_useful|outdated)$")
    citation_id: int | None = None
    comment: str = ""
    suggested_answer: str = ""


def _serialize_version(db, version: dict, user: dict, document: dict) -> dict:
    payload = {
        "id": version["id"],
        "document_id": version["document_id"],
        "version_no": version["version_no"],
        "filename": version["original_filename"],
        "status": version["status"],
        "validity_status": version["validity_status"],
        "created_at": version["created_at"],
        "superseded_at": version["superseded_at"],
        "content_hash": version["content_hash"],
    }
    # Uploader identity is visible only to the owner and administrators.
    is_owner_or_admin = user.get("is_admin") or (
        document is not None and document.get("owner_id") == user.get("id")
    )
    if is_owner_or_admin:
        payload["uploaded_by"] = version["uploaded_by"]
    return payload


@router.get("/documents/{document_id}/versions")
def list_document_versions(
    document_id: int, request: Request, user: dict = Depends(require_user)
) -> dict:
    """Return logical and version metadata only; never physical paths."""
    db = request.app.state.user_db
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_doc_view(db, document["doc_name"], document["kb_id"], user)
    versions = db.list_document_versions(document_id)
    return {
        "document_id": document_id,
        "versions": [_serialize_version(db, v, user, document) for v in versions],
    }


@router.get("/documents/{document_id}/versions/{version_id}/download")
def download_document_version(
    document_id: int,
    version_id: int,
    request: Request,
    user: dict = Depends(require_user),
):
    """Stream an immutable version file to an authorized viewer."""
    from pathlib import Path

    from fastapi.responses import FileResponse

    from rag.document_version_store import resolve_version_path

    db = request.app.state.user_db
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_doc_view(db, document["doc_name"], document["kb_id"], user)
    version = db.get_document_version_by_id(version_id)
    if version is None or version["document_id"] != document_id:
        raise HTTPException(status_code=404, detail="版本不存在")
    root = Path(getattr(request.app.state, "document_versions_root", "."))
    path = resolve_version_path(root, version["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="版本文件缺失")
    return FileResponse(path, filename=version["original_filename"])


@router.post("/feedback/knowledge")
def submit_feedback_knowledge(
    req: KnowledgeFeedbackRequest,
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    from rag.knowledge_feedback import (
        FeedbackValidationError,
        submit_knowledge_feedback,
    )

    db = request.app.state.user_db
    try:
        return submit_knowledge_feedback(
            db,
            user,
            req.assistant_message_id,
            req.kind,
            req.citation_id,
            req.comment,
            req.suggested_answer,
        )
    except FeedbackValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/knowledge-operations/my-feedback")
def my_feedback(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_user),
) -> dict:
    """Return only the current user's own feedback rows."""
    db = request.app.state.user_db
    items, total = db.list_user_feedback(user["id"], page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}
