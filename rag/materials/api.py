"""Focused material router: upload, jobs, archives, recycle, locate, organize.

Every request model forbids extra fields. The router authenticates first,
caps request size through streamed staging, delegates authorization to the
services, and maps stable errors to ``400/403/404/409/413/422/429/503``.
Exception strings and physical paths never leak into responses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from rag.api_security import (
    get_user_db,
    require_doc_edit,
    require_doc_view,
    require_kb_edit,
    require_user,
)
from rag.document_versions import VersionPromotionError, promote_staged_version
from rag.generator import StructuredOutputError
from rag.kb_lifecycle import KBLifecycleError, assert_kb_available, kb_mutation
from rag.kb_lifecycle_http import lifecycle_http_error
from rag.materials.archives import (
    ArchiveConfirmationError,
    ArchiveLimits,
    ArchiveService,
    confirm_archive_import,
)
from rag.materials.formats import build_default_format_registry
from rag.materials.jobs import enqueue_latest_index_job
from rag.materials.locator import MaterialLocator, material_result_response
from rag.materials.models import MaterialLimits, MaterialLocationQuery
from rag.materials.organizer import (
    OrganizationError,
    OrganizationInstruction,
    OrganizationService,
)
from rag.materials.recycle import RecycleError, delete_to_recycle, restore_from_recycle
from rag.materials.staging import MaterialTooLarge, stream_upload_to_stage
from rag.org_auth.audit import OrgAuditService
from rag.org_auth.models import OrgRole
from rag.org_auth.repository import OrgRepository, is_org_policy
from rag.org_auth.service import OrgAuthorizationService

router = APIRouter()

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024


class ArchiveConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_entry_ids: tuple[int, ...] = ()
    expected_version: int = 0
    idempotency_key: str = ""


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_metadata_version: int = 0
    idempotency_key: str = ""


class OrganizationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    document_ids: tuple[int, ...] = ()


class OrganizationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_item_ids: tuple[int, ...] = ()
    idempotency_key: str = ""


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = ""


def _versions_root(request: Request) -> Path:
    from config import settings

    return Path(getattr(request.app.state, "document_versions_root", settings.document_versions_root))


def _kb_id(request: Request) -> str:
    return request.query_params.get("kb_id", "rag_docs")


def _limits() -> MaterialLimits:
    return MaterialLimits()


def _archive_limits() -> ArchiveLimits:
    return ArchiveLimits()


def _audit_org_material(db, user: dict, action: str, kb_id: str) -> None:
    if is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        OrgAuditService(db).record(
            int(user["id"]), action, "knowledge_base", kb_id,
            "success", "authorized",
        )


def _read_signature(path: Path) -> bytes:
    import contextlib

    with contextlib.suppress(OSError), open(path, "rb") as fh:
        return fh.read(16)
    return b""


async def _upload_iterator(file: UploadFile):
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        yield chunk


def _raise_archive_error(exc: ArchiveConfirmationError):
    status = {
        "no_edit_permission": 403,
        "manifest_not_found": 404,
        "container_missing": 404,
        "container_version_missing": 404,
        "source_missing": 404,
        "manifest_not_pending": 409,
        "manifest_expired": 409,
        "version_conflict": 409,
        "no_approved_entries": 422,
    }.get(str(exc), 400)
    raise HTTPException(status_code=status, detail=str(exc)) from exc


def _raise_recycle_error(exc: RecycleError):
    status = {
        "no_edit_permission": 403,
        "no_restore_permission": 403,
        "document_missing": 404,
        "already_deleted": 409,
        "not_deleted": 409,
        "metadata_conflict": 409,
        "restore_window_expired": 409,
        "restore_window_open": 409,
        "not_tombstoned": 409,
        "already_purged": 409,
    }.get(exc.reason_code, 400)
    raise HTTPException(status_code=status, detail=exc.reason_code) from exc


def _raise_organization_error(exc: OrganizationError):
    status = {
        "no_confirm_permission": 403,
        "no_rollback_permission": 403,
        "proposal_missing": 404,
        "batch_missing": 404,
        "proposal_not_pending": 409,
        "proposal_expired": 409,
    }.get(exc.reason_code, 400)
    raise HTTPException(status_code=status, detail=exc.reason_code) from exc


def _semantic_retriever(db):
    def retrieve(query: str, allowed_version_ids: set[int], top_k: int):
        import contextlib

        with contextlib.suppress(Exception):
            from rag.embedder import embed
            from rag.vector_store import search_collection

            vector = embed([query])[0]
            return search_collection(
                "rag_docs", vector, top_k=top_k, allowed_version_ids=allowed_version_ids
            )
        return []

    return retrieve


@router.get("/materials/capabilities")
async def material_capabilities(user: dict = Depends(require_user)):
    registry = build_default_format_registry()
    capabilities = [
        {
            "key": capability.key,
            "extensions": sorted(capability.extensions),
            "mime_types": sorted(capability.mime_types),
            "archive": capability.archive,
            "preview": capability.preview,
            "extract_text": capability.extract_text,
            "local_ocr": capability.local_ocr,
            "structured_query": capability.structured_query,
            "semantic_index": capability.semantic_index,
            "archive_container": capability.archive_container,
            "citation_kind": capability.citation_kind,
        }
        for capability in registry._by_key.values()
    ]
    return {"capabilities": capabilities, "limits": _limits().model_dump()}


@router.post("/materials/upload", status_code=202)
async def upload_material(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_user),
):
    db = get_user_db(request)
    kb_id = _kb_id(request)
    if kb_id != "rag_docs":
        require_kb_edit(db, kb_id, user)
    try:
        assert_kb_available(db, kb_id)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    registry = build_default_format_registry()
    limits = _limits()
    try:
        staged = await stream_upload_to_stage(
            _upload_iterator(file),
            file.filename or "upload.bin",
            file.content_type,
            _versions_root(request),
            registry,
            limits,
            kb_id=kb_id if kb_id != "rag_docs" else None,
        )
    except MaterialTooLarge as exc:
        raise HTTPException(status_code=413, detail="material_too_large") from exc

    resolution = registry.resolve(staged.filename, staged.detected_mime, _read_signature(staged.path))
    if resolution.capability is not None and resolution.capability.archive_container:
        service = ArchiveService(db, _versions_root(request), registry, _archive_limits())
        try:
            preview = await asyncio.to_thread(
                service.upload_zip,
                staged.filename,
                kb_id,
                user,
                staged.path,
            )
        except KBLifecycleError as exc:
            staged.path.unlink(missing_ok=True)
            raise lifecycle_http_error(exc) from exc
        finally:
            staged.path.unlink(missing_ok=True)
        _audit_org_material(db, user, "archive_uploaded", kb_id)
        return {
            "status": "awaiting_archive_confirmation",
            "manifest_id": preview.manifest_id,
            "filename": staged.filename,
        }

    def _promote_and_enqueue():
        with kb_mutation(db, kb_id):
            if kb_id != "rag_docs":
                require_kb_edit(db, kb_id, user)
            existing = db.get_document_permission(staged.filename, kb_id)
            created_document = existing is None
            if existing is None:
                document_id = db.create_document_permission(
                    staged.filename, kb_id, user["id"], 1, False
                )
            else:
                document_id = existing["id"]
            try:
                promoted = promote_staged_version(
                    db,
                    _versions_root(request),
                    document_id=document_id,
                    filename=staged.filename,
                    staged_path=staged.path,
                    content_hash=staged.sha256,
                    uploaded_by=user["id"],
                )
            except VersionPromotionError as exc:
                if created_document:
                    try:
                        db.delete_document_permission(document_id)
                    except Exception:
                        logger.exception("material promotion rollback failed")
                raise HTTPException(status_code=500, detail="material_promotion_failed") from exc
            enqueue_latest_index_job(
                db, document_id, promoted.version_id, staged.sha256, time.time()
            )
            return document_id, promoted

    preserve_staged_upload = False
    try:
        document_id, promoted = await asyncio.to_thread(_promote_and_enqueue)
    except KBLifecycleError as exc:
        staged.path.unlink(missing_ok=True)
        raise lifecycle_http_error(exc) from exc
    except HTTPException as exc:
        if exc.status_code == 500 and exc.detail == "material_promotion_failed":
            preserve_staged_upload = True
        raise
    finally:
        if not preserve_staged_upload:
            staged.path.unlink(missing_ok=True)
    _audit_org_material(db, user, "document_uploaded", kb_id)
    return {
        "status": "queued",
        "document_id": document_id,
        "version_id": promoted.version_id,
        "filename": staged.filename,
        "content_hash": staged.sha256,
    }


@router.get("/materials/jobs/{job_id}")
async def get_material_job(request: Request, job_id: int, user: dict = Depends(require_user)):
    db = get_user_db(request)
    job = db.get_index_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    document = db.get_document_permission_by_id(job["document_id"])
    if document is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    require_doc_view(db, document["doc_name"], document["kb_id"], user)
    return {
        "job_id": job["id"],
        "document_id": job["document_id"],
        "version_id": job["version_id"],
        "status": job["status"],
        "error_code": job["error_code"],
        "updated_at": job["updated_at"],
    }


@router.get("/materials/archives/{manifest_id}")
async def get_archive_manifest(request: Request, manifest_id: int, user: dict = Depends(require_user)):
    db = get_user_db(request)
    manifest = db.get_material_archive_manifest(manifest_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="manifest_not_found")
    container = db.get_document_permission_by_id(manifest["container_document_id"])
    if container is None:
        # Archive manifests reveal unimported child names; do not expose them
        # to a shared-document viewer or disclose whether the container exists.
        raise HTTPException(status_code=404, detail="manifest_not_found")
    try:
        if container["kb_id"] != "rag_docs":
            require_kb_edit(db, container["kb_id"], user)
        else:
            require_doc_edit(db, container["doc_name"], container["kb_id"], user)
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail="manifest_not_found") from exc
    entries = db.list_material_archive_entries(manifest_id)
    # CAS token for ``POST /materials/archives/{id}/confirm``: the confirm route
    # compares ``expected_version`` against the container's current version_no
    # (0 while the container has no live version yet).  Returning it here lets a
    # client confirm without guessing/omitting the optimistic-concurrency value.
    current_container_version = db.get_current_document_version(
        manifest["container_document_id"]
    )
    return {
        "manifest_id": manifest["id"],
        "container_document_id": manifest["container_document_id"],
        "status": manifest["status"],
        "entry_count": manifest["entry_count"],
        "expires_at": manifest["expires_at"],
        "expected_version": (
            int(current_container_version["version_no"])
            if current_container_version is not None
            else 0
        ),
        "entries": [
            {
                "id": entry["id"],
                "relative_path": entry["relative_path"],
                "normalized_path": entry["normalized_path"],
                "size_bytes": entry["size_bytes"],
                "state": entry["state"],
                "reason_code": entry["reason_code"],
            }
            for entry in entries
        ],
    }


@router.post("/materials/archives/{manifest_id}/confirm")
async def confirm_archive(
    request: Request,
    manifest_id: int,
    body: ArchiveConfirmRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db(request)
    manifest = db.get_material_archive_manifest(manifest_id)
    container = db.get_document_permission_by_id(manifest["container_document_id"]) if manifest else None
    if container is None:
        raise HTTPException(status_code=404, detail="manifest_not_found")
    if container["kb_id"] != "rag_docs":
        require_kb_edit(db, container["kb_id"], user)
    else:
        require_doc_edit(db, container["doc_name"], container["kb_id"], user)
    try:
        result = await asyncio.to_thread(
            confirm_archive_import,
            db,
            manifest_id,
            user,
            tuple(body.selected_entry_ids),
            body.expected_version,
            body.idempotency_key,
            _versions_root(request),
            build_default_format_registry(),
            _archive_limits(),
        )
    except ArchiveConfirmationError as exc:
        _raise_archive_error(exc)
    except KBLifecycleError as exc:
        raise lifecycle_http_error(exc) from exc
    _audit_org_material(db, user, "archive_confirmed", container["kb_id"])
    return {
        "status": result.status,
        "container_document_id": result.container_document_id,
        "container_version_id": result.container_version_id,
        "created_children": list(result.created_children),
        "unchanged_children": list(result.unchanged_children),
        "rejected_entries": list(result.rejected_entries),
    }


@router.delete("/materials/{document_id}")
async def delete_material(request: Request, document_id: int, user: dict = Depends(require_user)):
    db = get_user_db(request)
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document_missing")
    policy = OrgRepository(db).get_kb_policy(document["kb_id"])
    organization_authorized = is_org_policy(policy)
    if document["kb_id"] != "rag_docs":
        require_kb_edit(db, document["kb_id"], user)
        organization_authorized = True
    elif not (
        user.get("is_admin")
        or user["id"] in {document["owner_id"], document.get("deleted_by")}
    ):
        raise HTTPException(status_code=403, detail="no_restore_permission")
    try:
        record = delete_to_recycle(
            db, document_id, user, organization_authorized=organization_authorized
        )
    except RecycleError as exc:
        _raise_recycle_error(exc)
    _audit_org_material(db, user, "document_recycled", document["kb_id"])
    return {
        "status": record.status,
        "document_id": record.document_id,
        "restore_until": record.restore_until,
    }


@router.post("/materials/{document_id}/restore")
async def restore_material(
    request: Request,
    document_id: int,
    body: RestoreRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db(request)
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document_missing")
    policy = OrgRepository(db).get_kb_policy(document["kb_id"])
    organization_authorized = is_org_policy(policy)
    if document["kb_id"] != "rag_docs":
        require_kb_edit(db, document["kb_id"], user)
        organization_authorized = True
    elif not (
        user.get("is_admin")
        or user["id"] in {document["owner_id"], document.get("deleted_by")}
    ):
        raise HTTPException(status_code=403, detail="no_restore_permission")
    try:
        result = restore_from_recycle(
            db, document_id, user, body.expected_metadata_version,
            organization_authorized=organization_authorized,
        )
    except RecycleError as exc:
        _raise_recycle_error(exc)
    _audit_org_material(db, user, "document_restored", document["kb_id"])
    return {
        "status": result.status,
        "document_id": result.document_id,
        "metadata_version": result.metadata_version,
    }


@router.get("/materials/recycle")
async def list_recycle(request: Request, user: dict = Depends(require_user)):
    db = get_user_db(request)
    items = db.list_recycled_documents()
    visible = []
    for item in items:
        policy = OrgRepository(db).get_kb_policy(item["kb_id"])
        if is_org_policy(policy):
            if OrgAuthorizationService(db).authorize(
                int(user["id"]), item["kb_id"], OrgRole.MANAGE
            ).allowed:
                visible.append(item)
        elif user.get("is_admin") or item["owner_id"] == user["id"] or item["deleted_by"] == user["id"]:
            visible.append(item)
    items = visible
    # The restore endpoint is CAS-guarded by ``expected_metadata_version``.
    # ``list_recycled_documents`` does not project that column, so read the few
    # visible tombstones' current version once each and surface it to the client
    # instead of requiring an omitted (or guessed) CAS value.
    metadata_versions: dict[int, int] = {}
    for item in items:
        document = db.get_document_permission_by_id(item["id"])
        metadata_versions[item["id"]] = (
            int(document["metadata_version"]) if document is not None else 0
        )
    return {
        "items": [
            {
                "document_id": item["id"],
                "filename": item["doc_name"],
                "deleted_at": item["deleted_at"],
                "deleted_by": item["deleted_by"],
                "restore_until": item["restore_until"],
                "metadata_version": metadata_versions[item["id"]],
            }
            for item in items
        ]
    }


@router.post("/materials/locate")
async def locate_material(
    request: Request,
    query: MaterialLocationQuery,
    user: dict = Depends(require_user),
):
    db = get_user_db(request)
    locator = MaterialLocator(db, semantic_retriever=_semantic_retriever(db))
    results = locator.locate(query, user)
    return {"results": [material_result_response(result, user, db) for result in results]}


@router.post("/materials/organization/preview")
async def organization_preview(
    request: Request,
    body: OrganizationPreviewRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db(request)
    service = OrganizationService(db)
    try:
        preview = service.preview(
            OrganizationInstruction(text=body.text, document_ids=body.document_ids), user
        )
    except OrganizationError as exc:
        _raise_organization_error(exc)
    except StructuredOutputError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    for item in preview.items:
        document = db.get_document_permission_by_id(item.document_id)
        if document is not None:
            _audit_org_material(db, user, "organization_previewed", document["kb_id"])
    return {
        "proposal_id": preview.id,
        "items": [
            {
                "id": item.id,
                "document_id": item.document_id,
                "target_path": item.target_path,
                "document_type": item.document_type,
                "status": item.status,
                "reason_code": item.reason_code,
                "confidence": item.confidence,
            }
            for item in preview.items
        ],
    }


@router.post("/materials/organization/{proposal_id}/confirm")
async def organization_confirm(
    request: Request,
    proposal_id: int,
    body: OrganizationConfirmRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db(request)
    service = OrganizationService(db)
    try:
        result = service.confirm(
            proposal_id, tuple(body.selected_item_ids), user, None, body.idempotency_key
        )
    except OrganizationError as exc:
        _raise_organization_error(exc)
    for item in result.items:
        document = db.get_document_permission_by_id(item.document_id)
        if document is not None:
            _audit_org_material(db, user, "organization_confirmed", document["kb_id"])
    return {
        "batch_id": result.batch_id,
        "status": result.status,
        "items": [
            {
                "id": item.id,
                "document_id": item.document_id,
                "status": item.status,
                "reason_code": item.reason_code,
            }
            for item in result.items
        ],
    }


@router.post("/materials/organization/batches/{batch_id}/rollback")
async def organization_rollback(
    request: Request,
    batch_id: int,
    body: RollbackRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db(request)
    service = OrganizationService(db)
    try:
        result = service.rollback(batch_id, user, body.idempotency_key)
    except OrganizationError as exc:
        _raise_organization_error(exc)
    for item in result.items:
        document = db.get_document_permission_by_id(item.document_id)
        if document is not None:
            _audit_org_material(db, user, "organization_rolled_back", document["kb_id"])
    return {
        "batch_id": result.batch_id,
        "status": result.status,
        "items": [
            {
                "id": item.id,
                "document_id": item.document_id,
                "status": item.status,
                "reason_code": item.reason_code,
            }
            for item in result.items
        ],
    }
