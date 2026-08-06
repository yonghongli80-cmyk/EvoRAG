"""Knowledge operations router: versions, feedback, tasks.

Extended by Tasks 5-7. Mounted exactly once by ``rag.api``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from rag.api_security import require_doc_view, require_user

router = APIRouter()


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
