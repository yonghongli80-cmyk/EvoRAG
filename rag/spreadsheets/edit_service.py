"""Optimistic spreadsheet edit orchestration for immutable document versions."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from rag.api_security import require_doc_version_edit
from rag.document_version_store import (
    final_version_path,
    promote_version,
    resolve_version_path,
)
from rag.kb_lifecycle import kb_mutation
from rag.materials.jobs import enqueue_latest_index_job
from rag.spreadsheets.audit import compute_risk_flags, document_sensitive_domain
from rag.spreadsheets.edit_models import (
    RequestMeta,
    SpreadsheetEditAccepted,
    SpreadsheetEditRequest,
    SpreadsheetEditUnchanged,
    SpreadsheetRestoreRequest,
    SpreadsheetStructureEditRequest,
)
from rag.spreadsheets.structure_editor import (
    WorkbookStructureError,
    apply_structure_edit,
)
from rag.spreadsheets.workbook_editor import (
    CellChange,
    WorkbookEditError,
    apply_cell_patches,
)
from rag.user_db import UserDB, VersionConflict

EnqueueIndexJob = Callable[[UserDB, int, int, str, float], int]


class SpreadsheetEditServiceError(Exception):
    def __init__(self, reason_code: str, status_code: int = 422) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.status_code = status_code


class SpreadsheetEditService:
    def __init__(
        self,
        db: UserDB,
        versions_root: Path,
        structured_datasets_root: Path,
        *,
        enqueue: EnqueueIndexJob | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.versions_root = Path(versions_root)
        self.structured_datasets_root = Path(structured_datasets_root)
        self.enqueue = enqueue or enqueue_latest_index_job
        self.clock = clock

    def save(
        self,
        document_id: int,
        actor: dict,
        request: SpreadsheetEditRequest,
        request_meta: RequestMeta,
    ) -> SpreadsheetEditAccepted | SpreadsheetEditUnchanged:
        document = self.db.get_document_permission_by_id(document_id)
        if document is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="document not found")
        with kb_mutation(self.db, document["kb_id"]):
            return self._save(document_id, actor, request, request_meta)

    def _save(
        self,
        document_id: int,
        actor: dict,
        request: SpreadsheetEditRequest,
        request_meta: RequestMeta,
    ) -> SpreadsheetEditAccepted | SpreadsheetEditUnchanged:
        if self.db.get_document_permission_by_id(document_id) is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="document not found")
        document = require_doc_version_edit(self.db, document_id, actor)
        if Path(document["doc_name"]).suffix.lower() != ".xlsx":
            raise SpreadsheetEditServiceError("unsupported_workbook_format", 422)
        current = self.db.get_current_document_version(document_id)
        if current is None or current["id"] != request.base_version_id:
            raise VersionConflict("spreadsheet_base_version_conflict")

        source = resolve_version_path(self.versions_root, current["file_path"])
        if not source.is_file():
            raise SpreadsheetEditServiceError("spreadsheet_version_missing", 404)
        sheet_map = self._sheet_map(document_id, current["id"])
        staging = (
            self.versions_root
            / "staging"
            / str(document_id)
            / f"spreadsheet-{uuid.uuid4().hex}.xlsx"
        )
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            changes = apply_cell_patches(
                source, staging, sheet_map, request.changes
            )
        except WorkbookEditError as exc:
            self._record_rejection(
                document,
                actor,
                request.base_version_id,
                request.reason,
                request_meta,
                exc.reason_code,
            )
            raise SpreadsheetEditServiceError(exc.reason_code, 422) from exc

        if all(self._same_value(change) for change in changes):
            staging.unlink(missing_ok=True)
            return SpreadsheetEditUnchanged()

        content_hash = hashlib.sha256(staging.read_bytes()).hexdigest()
        # D-09: record the sensitive-domain flag while the document is known, so
        # the audit trail itself marks values that a later viewer may not see.
        sensitive = 1 if document_sensitive_domain(self.db, document["kb_id"]) is not None else 0
        audit_changes = tuple(
            self._audit_change(change, sensitive) for change in changes
        )
        recent = self.db.count_recent_spreadsheet_edits(
            actor["id"], document_id, self.clock() - 600
        )
        risk_flags = compute_risk_flags(
            "edit", audit_changes, recent + 1, document
        )
        try:
            session_id, version_id = self.db.reserve_spreadsheet_edit(
                document_id=document_id,
                base_version_id=request.base_version_id,
                actor_user_id=actor["id"],
                original_filename=document["doc_name"],
                staged_path=str(staging),
                content_hash=content_hash,
                operation="edit",
                reason=request.reason,
                restored_from_version_id=None,
                changes=audit_changes,
                ip_address=request_meta.ip_address,
                user_agent=request_meta.user_agent,
                risk_flags=risk_flags,
            )
        except Exception:
            staging.unlink(missing_ok=True)
            raise

        destination: Path | None = None
        try:
            version = self.db.get_document_version_by_id(version_id)
            if version is None:
                raise RuntimeError("reserved version missing")
            destination = final_version_path(
                self.versions_root,
                document_id,
                version["version_no"],
                document["doc_name"],
            )
            promote_version(staging, destination)
            self.db.set_version_file_path(version_id, str(destination))
            self.enqueue(
                self.db,
                document_id,
                version_id,
                content_hash,
                self.clock(),
            )
        except Exception as exc:
            staging.unlink(missing_ok=True)
            if destination is not None:
                destination.unlink(missing_ok=True)
            self._mark_failed(session_id, version_id)
            raise SpreadsheetEditServiceError("spreadsheet_edit_enqueue_failed", 500) from exc
        return SpreadsheetEditAccepted(
            session_id=session_id,
            version_id=version_id,
        )

    def save_structure(
        self,
        document_id: int,
        actor: dict,
        request: SpreadsheetStructureEditRequest,
        request_meta: RequestMeta,
    ) -> SpreadsheetEditAccepted:
        document = self.db.get_document_permission_by_id(document_id)
        if document is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="document not found")
        with kb_mutation(self.db, document["kb_id"]):
            return self._save_structure(document_id, actor, request, request_meta)

    def _save_structure(
        self,
        document_id: int,
        actor: dict,
        request: SpreadsheetStructureEditRequest,
        request_meta: RequestMeta,
    ) -> SpreadsheetEditAccepted:
        """Apply one structure edit through the immutable version pipeline."""
        if self.db.get_document_permission_by_id(document_id) is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="document not found")
        document = require_doc_version_edit(self.db, document_id, actor)
        if Path(document["doc_name"]).suffix.lower() != ".xlsx":
            raise SpreadsheetEditServiceError("unsupported_workbook_format", 422)
        current = self.db.get_current_document_version(document_id)
        if current is None or current["id"] != request.base_version_id:
            raise VersionConflict("spreadsheet_base_version_conflict")

        source = resolve_version_path(self.versions_root, current["file_path"])
        if not source.is_file():
            raise SpreadsheetEditServiceError("spreadsheet_version_missing", 404)
        sheet_map = self._sheet_map(document_id, current["id"])
        staging = (
            self.versions_root
            / "staging"
            / str(document_id)
            / f"spreadsheet-structure-{uuid.uuid4().hex}.xlsx"
        )
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            change = apply_structure_edit(
                source,
                staging,
                sheet_map,
                request,
            )
        except WorkbookStructureError as exc:
            self._record_rejection(
                document,
                actor,
                request.base_version_id,
                request.reason,
                request_meta,
                exc.reason_code,
            )
            raise SpreadsheetEditServiceError(exc.reason_code, 422) from exc

        content_hash = hashlib.sha256(staging.read_bytes()).hexdigest()
        recent = self.db.count_recent_spreadsheet_edits(
            actor["id"], document_id, self.clock() - 600
        )
        risk_flags = compute_risk_flags("edit", (), recent + 1, document)
        if request.action.startswith("delete_"):
            risk_flags = tuple(
                sorted(set(risk_flags) | {"destructive_structure_edit"})
            )
        structure_change = {
            "action": change.action,
            "sheet_key": change.sheet_key,
            "sheet_name_snapshot": change.sheet_name,
            "target_index": change.index,
            "count": change.count,
            "new_sheet_name": change.new_sheet_name,
            "affected_nonempty_cells": change.affected_nonempty_cells,
            "confirm_nonempty": request.confirm_nonempty,
        }
        try:
            session_id, version_id = self.db.reserve_spreadsheet_structure_edit(
                document_id=document_id,
                base_version_id=request.base_version_id,
                actor_user_id=actor["id"],
                original_filename=document["doc_name"],
                staged_path=str(staging),
                content_hash=content_hash,
                reason=request.reason,
                structure_change=structure_change,
                ip_address=request_meta.ip_address,
                user_agent=request_meta.user_agent,
                risk_flags=risk_flags,
            )
        except Exception:
            staging.unlink(missing_ok=True)
            raise

        destination: Path | None = None
        try:
            version = self.db.get_document_version_by_id(version_id)
            if version is None:
                raise RuntimeError("reserved version missing")
            destination = final_version_path(
                self.versions_root,
                document_id,
                version["version_no"],
                document["doc_name"],
            )
            promote_version(staging, destination)
            self.db.set_version_file_path(version_id, str(destination))
            self.enqueue(
                self.db,
                document_id,
                version_id,
                content_hash,
                self.clock(),
            )
        except Exception as exc:
            staging.unlink(missing_ok=True)
            if destination is not None:
                destination.unlink(missing_ok=True)
            self._mark_failed(session_id, version_id)
            raise SpreadsheetEditServiceError(
                "spreadsheet_edit_enqueue_failed", 500
            ) from exc
        return SpreadsheetEditAccepted(
            session_id=session_id,
            version_id=version_id,
        )

    def restore(
        self,
        document_id: int,
        source_version_id: int,
        actor: dict,
        request: SpreadsheetRestoreRequest,
        request_meta: RequestMeta,
    ) -> SpreadsheetEditAccepted:
        document = self.db.get_document_permission_by_id(document_id)
        if document is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="document not found")
        with kb_mutation(self.db, document["kb_id"]):
            return self._restore(
                document_id,
                source_version_id,
                actor,
                request,
                request_meta,
            )

    def _restore(
        self,
        document_id: int,
        source_version_id: int,
        actor: dict,
        request: SpreadsheetRestoreRequest,
        request_meta: RequestMeta,
    ) -> SpreadsheetEditAccepted:
        if self.db.get_document_permission_by_id(document_id) is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="document not found")
        document = require_doc_version_edit(self.db, document_id, actor)
        if Path(document["doc_name"]).suffix.lower() != ".xlsx":
            raise SpreadsheetEditServiceError("unsupported_workbook_format", 422)
        current = self.db.get_current_document_version(document_id)
        if current is None or current["id"] != request.base_version_id:
            raise VersionConflict("spreadsheet_base_version_conflict")
        source_version = self.db.get_document_version_by_id(source_version_id)
        if (
            source_version is None
            or source_version["document_id"] != document_id
            or source_version["status"] != "historical"
        ):
            raise VersionConflict("spreadsheet_restore_source_conflict")
        source = resolve_version_path(
            self.versions_root, source_version["file_path"]
        )
        if not source.is_file():
            raise SpreadsheetEditServiceError("spreadsheet_version_missing", 404)

        staging = (
            self.versions_root
            / "staging"
            / str(document_id)
            / f"restore-{uuid.uuid4().hex}.xlsx"
        )
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, staging)
            validation_book = load_workbook(
                staging,
                data_only=False,
                read_only=True,
                keep_links=True,
            )
            validation_book.close()
        except Exception as exc:
            staging.unlink(missing_ok=True)
            raise SpreadsheetEditServiceError("workbook_validation_failed", 422) from exc

        content_hash = hashlib.sha256(staging.read_bytes()).hexdigest()
        risk_flags = ()
        if source_version["version_no"] != current["version_no"] - 1:
            risk_flags = ("non_previous_restore",)
        recent = self.db.count_recent_spreadsheet_edits(
            actor["id"], document_id, self.clock() - 600
        )
        risk_flags = tuple(
            sorted(
                set(risk_flags)
                | set(
                    compute_risk_flags(
                        "rollback",
                        (),
                        recent + 1,
                        {
                            **document,
                            "non_previous_restore": bool(risk_flags),
                        },
                    )
                )
            )
        )
        try:
            session_id, version_id = self.db.reserve_spreadsheet_edit(
                document_id=document_id,
                base_version_id=request.base_version_id,
                actor_user_id=actor["id"],
                original_filename=document["doc_name"],
                staged_path=str(staging),
                content_hash=content_hash,
                operation="rollback",
                reason=request.reason,
                restored_from_version_id=source_version_id,
                changes=(),
                ip_address=request_meta.ip_address,
                user_agent=request_meta.user_agent,
                risk_flags=risk_flags,
            )
        except Exception:
            staging.unlink(missing_ok=True)
            raise

        destination: Path | None = None
        try:
            version = self.db.get_document_version_by_id(version_id)
            if version is None:
                raise RuntimeError("reserved version missing")
            destination = final_version_path(
                self.versions_root,
                document_id,
                version["version_no"],
                document["doc_name"],
            )
            promote_version(staging, destination)
            self.db.set_version_file_path(version_id, str(destination))
            self.enqueue(
                self.db,
                document_id,
                version_id,
                content_hash,
                self.clock(),
            )
        except Exception as exc:
            staging.unlink(missing_ok=True)
            if destination is not None:
                destination.unlink(missing_ok=True)
            self._mark_failed(session_id, version_id)
            raise SpreadsheetEditServiceError("spreadsheet_restore_enqueue_failed", 500) from exc
        return SpreadsheetEditAccepted(
            session_id=session_id,
            version_id=version_id,
        )

    def _sheet_map(self, document_id: int, version_id: int) -> dict[str, str]:
        dataset = self.db.get_structured_dataset_for_version(version_id)
        if (
            dataset is None
            or dataset["document_id"] != document_id
            or dataset["status"] != "ready"
        ):
            raise SpreadsheetEditServiceError("spreadsheet_dataset_not_ready", 409)
        root = self.structured_datasets_root.resolve()
        path = (root / dataset["storage_key"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise SpreadsheetEditServiceError("spreadsheet_dataset_not_ready", 409)
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT sheet_key, name FROM sheet_manifest ORDER BY ordinal"
            ).fetchall()
        finally:
            connection.close()
        return {str(row[0]): str(row[1]) for row in rows}

    @staticmethod
    def _same_value(change: CellChange) -> bool:
        old = change.old_value
        new = change.new_value
        if isinstance(old, datetime) and isinstance(new, date) and not isinstance(new, datetime):
            return old.date() == new
        return type(old) is type(new) and old == new

    @staticmethod
    def _json_value(value: object | None) -> str:
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _audit_change(cls, change: CellChange, sensitive: int = 0) -> dict[str, object]:
        return {
            "sheet_key": change.sheet_key,
            "sheet_name_snapshot": change.sheet_name,
            "cell_address": change.cell_address,
            "old_value_type": change.old_value_type,
            "old_value_json": cls._json_value(change.old_value),
            "new_value_type": change.new_value_type,
            "new_value_json": cls._json_value(change.new_value),
            "sensitive": 1 if sensitive else 0,
        }

    def _mark_failed(self, session_id: int, version_id: int) -> None:
        try:
            self.db.set_spreadsheet_edit_status(
                session_id,
                "failed",
                error_code="spreadsheet_edit_enqueue_failed",
            )
        finally:
            self.db.set_version_status(version_id, "failed")

    def _record_rejection(
        self,
        document: dict,
        actor: dict,
        base_version_id: int,
        reason: str,
        request_meta: RequestMeta,
        error_code: str,
    ) -> None:
        flags = compute_risk_flags(
            "edit",
            (),
            0,
            {**document, "rejected_reason": error_code},
        )
        self.db.record_rejected_spreadsheet_edit(
            document_id=document["id"],
            base_version_id=base_version_id,
            actor_user_id=actor["id"],
            operation="edit",
            document_name_snapshot=document["doc_name"],
            reason=reason,
            error_code=error_code,
            risk_flags=flags,
            ip_address=request_meta.ip_address,
            user_agent=request_meta.user_agent,
        )
