"""Knowledge operations router: versions, feedback, tasks.

Extended by Tasks 5-7. Mounted exactly once by ``rag.api``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from rag.api_security import require_doc_view, require_personal_document_acl, require_user
from rag.org_auth.audit import OrgAuditService
from rag.org_auth.repository import OrgRepository, is_org_policy

router = APIRouter()


class KnowledgeFeedbackRequest(BaseModel):
    assistant_message_id: int
    kind: str = Field(pattern="^(useful|not_useful|outdated)$")
    citation_id: int | None = None
    comment: str = ""
    suggested_answer: str = ""


class OwnerChangeRequest(BaseModel):
    owner_id: int


class TaskTransitionRequest(BaseModel):
    status: str = Field(
        pattern="^(open|in_progress|resolved|dismissed|confirmed_expired)$"
    )
    note: str = ""
    expected_updated_at: float


class TaskAssigneeRequest(BaseModel):
    assignee_id: int | None = None


class ConflictResolutionRequest(BaseModel):
    resolution: str = Field(pattern="^(resolved|dismissed)$")
    note: str = ""


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
        "restored_from_version_id": version.get("restored_from_version_id"),
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
    if is_org_policy(OrgRepository(db).get_kb_policy(document["kb_id"])):
        OrgAuditService(db).record(
            int(user["id"]), "document_version_downloaded", "knowledge_base",
            document["kb_id"], "success", "authorized",
            {"document_id": document_id, "version_id": version_id},
        )
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


@router.put("/knowledge-operations/conflicts/{conflict_id}/resolve")
def resolve_knowledge_conflict(
    conflict_id: int,
    req: ConflictResolutionRequest,
    request: Request,
    user: dict = Depends(require_user),  # noqa: B008 - FastAPI dependency marker
) -> dict:
    from rag.reliability.conflicts import resolve_conflict

    try:
        resolved = resolve_conflict(
            request.app.state.user_db, conflict_id, user, req.resolution, req.note
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权处理该冲突") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="冲突不存在") from exc
    return {"id": resolved["id"], "status": resolved["status"]}


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


def _serialize_task(db, task: dict, user: dict) -> dict:
    payload = {
        "id": task["id"],
        "task_type": task["task_type"],
        "document_id": task["document_id"],
        "document_version_id": task["document_version_id"],
        "assigned_to": task["assigned_to"],
        "status": task["status"],
        "report_count": task["report_count"],
        "resolution_note": task["resolution_note"],
        "due_at": task["due_at"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "resolved_at": task["resolved_at"],
    }
    is_assignee = task.get("assigned_to") is not None and task["assigned_to"] == user.get("id")
    if is_assignee or user.get("is_admin"):
        payload["reporters"] = db.list_task_reporters(task["id"])
    return payload


@router.get("/knowledge-operations/my-tasks")
def my_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_user),
) -> dict:
    db = request.app.state.user_db
    items, total = db.list_tasks_for_user(user["id"], page, page_size)
    return {
        "items": [_serialize_task(db, t, user) for t in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/knowledge-operations/tasks")
def list_all_tasks(
    request: Request,
    status: str | None = Query(None),
    assignee_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_user),
) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    db = request.app.state.user_db
    items, total = db.list_tasks_admin(status, assignee_id, page, page_size)
    return {
        "items": [_serialize_task(db, t, user) for t in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/knowledge-operations/summary")
def operations_summary(request: Request, user: dict = Depends(require_user)) -> dict:
    return request.app.state.user_db.task_summary()


@router.put("/knowledge-operations/tasks/{task_id}")
def transition_task(
    task_id: int,
    req: TaskTransitionRequest,
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    from rag.knowledge_tasks import TaskService

    db = request.app.state.user_db
    try:
        task = TaskService(db).transition(
            task_id, user, req.status, req.note, req.expected_updated_at
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_task(db, task, user)


@router.put("/knowledge-operations/tasks/{task_id}/assignee")
def reassign_task(
    task_id: int,
    req: TaskAssigneeRequest,
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    from rag.knowledge_tasks import TaskService

    db = request.app.state.user_db
    task = TaskService(db).reassign(task_id, user, req.assignee_id)
    return _serialize_task(db, task, user)


@router.put("/documents/{document_id}/owner")
def change_document_owner(
    document_id: int,
    req: OwnerChangeRequest,
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    db = request.app.state.user_db
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    require_personal_document_acl(db, document["kb_id"])
    db.set_document_owner(document_id, req.owner_id)
    return {"document_id": document_id, "owner_id": req.owner_id}
