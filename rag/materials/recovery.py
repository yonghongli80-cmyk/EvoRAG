"""Explicit, audited recovery of two historical staging failure modes.

This module is a maintenance API.  It is deliberately not wired to an HTTP
route or the read-only staging audit command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from rag.api_security import require_doc_version_edit, require_kb_edit
from rag.kb_lifecycle import KBLifecycleError, assert_kb_available, kb_mutation
from rag.materials.recovery_files import (
    RecoveryFileError,
    confined_recovery_path,
    copy_recovery_file,
    verify_recovery_file,
)
from rag.org_auth.repository import OrgRepository
from rag.repositories.staging_recovery_repo import (
    RecoveryReceiptConflict,
    cas_recovery_paths,
    get_recovery_receipt,
    insert_recovery_receipt,
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$", re.ASCII)
_KINDS = {"version_pointer", "archive_rehome"}
_ACTIVE_JOBS = ("queued", "retry", "running")


@dataclass(frozen=True)
class RecoveryRequest:
    operation_key: str
    kind: str
    actor_user_id: int
    document_id: int
    version_id: int
    candidate_path: str = ""
    manifest_id: int | None = None


@dataclass(frozen=True)
class RecoveryPlan:
    request: RecoveryRequest
    versions_root: str
    source_path: str
    destination_path: str
    expected_version_path: str
    expected_manifest_source: str | None
    content_hash: str
    byte_size: int
    metadata_fingerprint: str
    request_hash: str


class RecoveryError(RuntimeError):
    """Stable maintenance error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _validate_request(request: RecoveryRequest) -> None:
    if not isinstance(request, RecoveryRequest):
        raise RecoveryError("invalid_request")
    if not isinstance(request.operation_key, str) or not _KEY_RE.fullmatch(request.operation_key):
        raise RecoveryError("invalid_operation_key")
    if not isinstance(request.kind, str) or request.kind not in _KINDS:
        raise RecoveryError("invalid_kind")
    for name, value in (
        ("actor_user_id", request.actor_user_id),
        ("document_id", request.document_id),
        ("version_id", request.version_id),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RecoveryError(f"invalid_{name}")
    if request.manifest_id is not None and (
        isinstance(request.manifest_id, bool) or not isinstance(request.manifest_id, int) or request.manifest_id <= 0
    ):
        raise RecoveryError("invalid_manifest_id")
    if not isinstance(request.candidate_path, str):
        raise RecoveryError("invalid_candidate_path")
    if request.kind == "version_pointer" and not request.candidate_path:
        raise RecoveryError("candidate_required")
    if request.kind == "version_pointer" and request.manifest_id is not None:
        raise RecoveryError("unexpected_manifest_id")
    if request.kind == "archive_rehome" and request.manifest_id is None:
        raise RecoveryError("manifest_required")


def _writer(db: Any) -> sqlite3.Connection:
    conn = db._conn
    if conn.in_transaction:
        raise RecoveryError("nested_transaction")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _row(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    item = conn.execute(sql, params).fetchone()
    return dict(item) if item is not None else None


def _require_actor_and_acl(db: Any, request: RecoveryRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    user = db.get_user_by_id(request.actor_user_id)
    if user is None:
        raise RecoveryError("actor_missing")
    person = OrgRepository(db).get_person_for_user(request.actor_user_id)
    if person is not None and not bool(person.get("active")):
        raise RecoveryError("actor_inactive")
    try:
        permission = require_doc_version_edit(db, request.document_id, user)
        if permission.get("deleted_at") is not None:
            raise RecoveryError("document_deleted")
        if permission.get("protected"):
            raise RecoveryError("document_protected")
        assert_kb_available(db, str(permission["kb_id"]))
        if permission.get("kb_id") != "rag_docs":
            require_kb_edit(db, permission["kb_id"], user)
    except RecoveryError:
        raise
    except HTTPException as exc:
        raise RecoveryError("authorization_denied") from exc
    return user, permission


def _file_identity(root: Path, path: str) -> Path:
    try:
        return confined_recovery_path(root, path)
    except RecoveryFileError as exc:
        raise RecoveryError(exc.code) from exc


def _check_hash_text(value: Any) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise RecoveryError("trusted_hash_invalid")
    return value


def _path_keys(value: Any, root: Path | None = None) -> set[str]:
    if not isinstance(value, str) or not value or "\x00" in value:
        if value in (None, ""):
            return set()
        raise RecoveryError("unsupported_path_metadata")
    try:
        keys = {
            os.path.normcase(os.path.normpath(os.path.abspath(value))),
            os.path.normcase(os.path.normpath(str(Path(value).resolve(strict=False)))),
        }
        if root is not None and not Path(value).is_absolute():
            rooted = root / value
            keys.add(os.path.normcase(os.path.normpath(os.path.abspath(str(rooted)))))
            keys.add(os.path.normcase(os.path.normpath(str(rooted.resolve(strict=False)))))
    except (OSError, RuntimeError, ValueError) as exc:
        raise RecoveryError("unsupported_path_metadata") from exc
    return keys


def _references(
    conn: sqlite3.Connection, request: RecoveryRequest, source: str, destination: str, root: Path
) -> dict[str, list[dict[str, Any]]]:
    path_keys = _path_keys(source, root) | _path_keys(destination, root)
    all_versions = [
        dict(r)
        for r in conn.execute("SELECT id, document_id, file_path, status FROM document_versions ORDER BY id").fetchall()
    ]
    version_refs = [r for r in all_versions if _path_keys(r["file_path"], root) & path_keys]
    # Only the target version's existing reference to its own source is necessary.
    bad_version_refs = [r for r in version_refs if r["id"] != request.version_id]
    current_refs = [
        dict(r)
        for r in conn.execute(
            "SELECT id, doc_name, kb_id, current_version_id FROM document_permissions "
            "WHERE current_version_id=? ORDER BY id",
            (request.version_id,),
        ).fetchall()
    ]
    kb_document_refs = [
        dict(r)
        for r in conn.execute(
            "SELECT id, kb_id, filename, file_path, document_id, document_version_id FROM kb_documents ORDER BY id",
        ).fetchall()
    ]
    kb_document_refs = [
        r
        for r in kb_document_refs
        if r["document_version_id"] == request.version_id or (_path_keys(r["file_path"], root) & path_keys)
    ]
    manifest_refs = [
        dict(r)
        for r in conn.execute(
            "SELECT id, container_document_id, container_version_id, source_path, status "
            "FROM material_archive_manifests ORDER BY id",
        ).fetchall()
    ]
    manifest_refs = [
        r
        for r in manifest_refs
        if r["container_version_id"] == request.version_id or (_path_keys(r["source_path"], root) & path_keys)
    ]
    bad_manifest_refs = [r for r in manifest_refs if r["id"] != request.manifest_id]
    if not _table_exists(conn, "document_index_jobs"):
        raise RecoveryError("unsupported_schema")
    jobs = [
        dict(r)
        for r in conn.execute(
            "SELECT id, document_id, version_id, status, job_type "
            "FROM document_index_jobs WHERE version_id=? AND status IN (?,?,?) ORDER BY id",
            (request.version_id, *_ACTIVE_JOBS),
        ).fetchall()
    ]
    if bad_version_refs or current_refs or kb_document_refs or bad_manifest_refs or jobs:
        raise RecoveryError("shared_reference")
    return {
        "version_paths": version_refs,
        "current_pointers": current_refs,
        "kb_documents": kb_document_refs,
        "manifests": manifest_refs,
        "active_jobs": jobs,
    }


def _acl_snapshot(conn: sqlite3.Connection, permission: dict[str, Any], actor_id: int) -> dict[str, Any]:
    kb_id = permission["kb_id"]
    snapshot: dict[str, Any] = {
        "permission": permission,
        "kb_metadata": _row(conn, "SELECT * FROM kb_metadata WHERE kb_id=?", (kb_id,)),
        "actor": _row(conn, "SELECT id, username, is_admin, permission_level FROM users WHERE id=?", (actor_id,)),
    }
    for table, query, args in (
        ("kb_org_policies", "SELECT * FROM kb_org_policies WHERE kb_id=?", (kb_id,)),
        ("kb_org_grants", "SELECT * FROM kb_org_grants WHERE kb_id=? ORDER BY rowid", (kb_id,)),
        ("org_people", "SELECT * FROM org_people WHERE user_id=?", (actor_id,)),
        ("org_units", "SELECT * FROM org_units ORDER BY code", ()),
    ):
        if _table_exists(conn, table):
            rows = [dict(row) for row in conn.execute(query, args).fetchall()]
            snapshot[table] = rows
    person = (snapshot.get("org_people") or [{}])[0]
    if person.get("person_code"):
        for table in ("org_memberships", "org_manager_scopes", "org_sensitive_qualifications"):
            if _table_exists(conn, table):
                snapshot[table] = [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} WHERE person_code=? ORDER BY rowid",
                        (person["person_code"],),
                    ).fetchall()
                ]
    return snapshot


def _build_plan(
    db: Any, conn: sqlite3.Connection, root: Path, request: RecoveryRequest, permission: dict[str, Any]
) -> RecoveryPlan:
    if not root.is_absolute() or not root.exists() or not root.is_dir():
        raise RecoveryError("invalid_versions_root")
    try:
        # The Task 1 helper validates the root itself too, including root
        # symlinks/junctions; don't resolve the caller's root before this check.
        confined_recovery_path(root, root)
    except (OSError, RecoveryFileError) as exc:
        raise RecoveryError("invalid_versions_root") from exc
    version = _row(
        conn, "SELECT * FROM document_versions WHERE id=? AND document_id=?", (request.version_id, request.document_id)
    )
    if version is None:
        raise RecoveryError("version_identity_mismatch")
    if version.get("status") != "staging":
        raise RecoveryError("version_not_staging")
    expected_version = str(version.get("file_path", ""))
    if not expected_version:
        raise RecoveryError("declared_path_missing")
    expected_hash = _check_hash_text(version.get("content_hash"))
    manifest: dict[str, Any] | None = None
    expected_manifest_source: str | None = None
    if request.kind == "version_pointer":
        source = _file_identity(root, request.candidate_path)
        if str(source) != request.candidate_path:
            raise RecoveryError("noncanonical_path")
        if source.parent != (root / str(request.document_id) / str(version["version_no"])):
            raise RecoveryError("candidate_outside_version_directory")
        declared = _file_identity(root, expected_version)
        if declared.exists():
            raise RecoveryError("declared_path_present")
        destination = source
    else:
        if permission.get("kb_id") == "rag_docs":
            raise RecoveryError("rag_docs_archive_rehome_forbidden")
        if not _table_exists(conn, "material_archive_manifests"):
            raise RecoveryError("unsupported_schema")
        manifest = _row(conn, "SELECT * FROM material_archive_manifests WHERE id=?", (request.manifest_id,))
        if manifest is None:
            raise RecoveryError("manifest_missing")
        if (
            manifest["container_document_id"] != request.document_id
            or manifest["container_version_id"] != request.version_id
        ):
            raise RecoveryError("manifest_identity_mismatch")
        if manifest["status"] != "pending_confirmation":
            raise RecoveryError("manifest_not_pending")
        if float(manifest["expires_at"] or 0) <= time.time():
            raise RecoveryError("manifest_expired")
        source = _file_identity(root, str(manifest["source_path"]))
        if source.parent != root / "staging":
            raise RecoveryError("archive_source_not_global_stage")
        expected_manifest_source = str(manifest["source_path"])
        base = _digest({"request": asdict(request), "root": str(root)})
        destination = (
            root
            / "staging"
            / "kb"
            / hashlib.sha256(str(permission["kb_id"]).encode("utf-8")).hexdigest()
            / f"recovery-{base}.bin"
        )

    try:
        source_file = verify_recovery_file(root, source, expected_hash)
    except RecoveryFileError as exc:
        raise RecoveryError(exc.code) from exc
    # Pointer repair is a repoint, never a copy. Archive rehome is an exclusive copy.
    try:
        confined_recovery_path(root, destination)
    except RecoveryFileError as exc:
        raise RecoveryError(exc.code) from exc

    refs = _references(conn, request, str(source), str(destination), root)
    writer_permission = _row(conn, "SELECT * FROM document_permissions WHERE id=?", (request.document_id,))
    if writer_permission is None or writer_permission.get("kb_id") != permission.get("kb_id"):
        raise RecoveryError("identity_changed")
    acl = _acl_snapshot(conn, writer_permission, request.actor_user_id)
    entries = []
    if manifest is not None and _table_exists(conn, "material_archive_entries"):
        entries = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM material_archive_entries WHERE manifest_id=? ORDER BY id",
                (request.manifest_id,),
            ).fetchall()
        ]
    fingerprint = _digest(
        {
            "permission": writer_permission,
            "version": version,
            "manifest": manifest,
            "manifest_entries": entries,
            "references": refs,
            "acl": acl,
        }
    )
    fields = {
        "request": asdict(request),
        "versions_root": str(root),
        "source_path": str(source),
        "destination_path": str(destination),
        "expected_version_path": expected_version,
        "expected_manifest_source": expected_manifest_source,
        "content_hash": source_file.sha256,
        "byte_size": source_file.byte_size,
        "metadata_fingerprint": fingerprint,
    }
    return RecoveryPlan(
        request=request,
        versions_root=str(root),
        source_path=str(source),
        destination_path=str(destination),
        expected_version_path=expected_version,
        expected_manifest_source=expected_manifest_source,
        content_hash=source_file.sha256,
        byte_size=source_file.byte_size,
        metadata_fingerprint=fingerprint,
        request_hash=_digest(fields),
    )


def preview_staging_recovery(db: Any, versions_root: Path, request: RecoveryRequest) -> RecoveryPlan:
    """Validate an explicitly supplied recovery without changing files or metadata."""
    _validate_request(request)
    conn = _writer(db)
    with db.cm.transaction():
        _user, permission = _require_actor_and_acl(db, request)
        return _build_plan(db, conn, Path(versions_root), request, permission)


def _same_plan(left: RecoveryPlan, right: RecoveryPlan) -> bool:
    return asdict(left) == asdict(right)


def _plan_hash(plan: RecoveryPlan) -> str:
    return _digest(
        {
            "request": asdict(plan.request),
            "versions_root": plan.versions_root,
            "source_path": plan.source_path,
            "destination_path": plan.destination_path,
            "expected_version_path": plan.expected_version_path,
            "expected_manifest_source": plan.expected_manifest_source,
            "content_hash": plan.content_hash,
            "byte_size": plan.byte_size,
            "metadata_fingerprint": plan.metadata_fingerprint,
        }
    )


def _replay_receipt(
    db: Any, conn: sqlite3.Connection, plan: RecoveryPlan, permission: dict[str, Any], receipt_row: dict[str, Any]
) -> dict[str, Any]:
    request = plan.request
    if _plan_hash(plan) != plan.request_hash or receipt_row["request_hash"] != plan.request_hash:
        raise RecoveryError("operation_key_conflict")
    receipt = receipt_row["receipt"]
    if (
        receipt.get("actor_user_id") != request.actor_user_id
        or receipt.get("document_id") != request.document_id
        or receipt.get("version_id") != request.version_id
        or receipt.get("operation_key") != request.operation_key
        or receipt.get("kind") != request.kind
        or receipt.get("manifest_id") != request.manifest_id
        or receipt.get("source_path") != plan.source_path
        or receipt.get("destination_path") != plan.destination_path
        or receipt.get("content_hash") != plan.content_hash
        or receipt.get("byte_size") != plan.byte_size
        or receipt.get("source_preserved") is not True
    ):
        raise RecoveryError("operation_key_conflict")
    if (
        receipt_row.get("actor_user_id") != request.actor_user_id
        or receipt_row.get("document_id") != request.document_id
        or receipt_row.get("version_id") != request.version_id
    ):
        raise RecoveryError("operation_key_conflict")
    version = _row(
        conn, "SELECT * FROM document_versions WHERE id=? AND document_id=?", (request.version_id, request.document_id)
    )
    if version is None:
        raise RecoveryError("replay_identity_changed")
    manifest = None
    if request.kind == "archive_rehome":
        manifest = _row(conn, "SELECT * FROM material_archive_manifests WHERE id=?", (request.manifest_id,))
        if manifest is None or manifest.get("source_path") != receipt.get("destination_path"):
            raise RecoveryError("replay_identity_changed")
    if version.get("status") != "staging" or version.get("content_hash") != receipt.get("content_hash"):
        raise RecoveryError("replay_identity_changed")
    if request.kind == "version_pointer" and version.get("file_path") != plan.destination_path:
        raise RecoveryError("replay_identity_changed")
    if request.kind == "archive_rehome" and version.get("file_path") not in {
        plan.expected_version_path,
        plan.destination_path,
    }:
        raise RecoveryError("replay_identity_changed")
    try:
        copied = verify_recovery_file(Path(plan.versions_root), receipt["destination_path"], receipt["content_hash"])
        original = verify_recovery_file(Path(plan.versions_root), receipt["source_path"], receipt["content_hash"])
    except RecoveryFileError as exc:
        raise RecoveryError(exc.code) from exc
    if copied.byte_size != receipt.get("byte_size") or original.byte_size != receipt.get("byte_size"):
        raise RecoveryError("replay_identity_changed")
    return receipt


def apply_staging_recovery(db: Any, plan: RecoveryPlan, *, maintenance_confirmed: bool = False) -> dict[str, Any]:
    """Apply a fresh, reviewed plan in one KB permit and SQLite transaction."""
    if maintenance_confirmed is not True:
        raise RecoveryError("maintenance_confirmation_required")
    if not isinstance(plan, RecoveryPlan):
        raise RecoveryError("invalid_plan")
    request = plan.request
    _validate_request(request)
    conn = _writer(db)
    # Acquire the lifecycle permit before the writer transaction; retain the
    # stable KBLifecycleError codes for callers.
    permission_now = db.get_document_permission_by_id(request.document_id)
    if permission_now is None:
        raise RecoveryError("authorization_denied")
    if _plan_hash(plan) != plan.request_hash:
        raise RecoveryError("invalid_plan")
    try:
        with kb_mutation(db, str(permission_now["kb_id"])), db.cm.transaction(immediate=True):
            _user, permission = _require_actor_and_acl(db, request)
            if permission.get("kb_id") != permission_now.get("kb_id"):
                raise RecoveryError("identity_changed")
            receipt_row = get_recovery_receipt(conn, request.operation_key)
            if receipt_row is not None:
                return _replay_receipt(db, conn, plan, permission, receipt_row)
            fresh = _build_plan(db, conn, Path(plan.versions_root), request, permission)
            if not _same_plan(plan, fresh):
                raise RecoveryError("stale_plan")
            if request.kind == "archive_rehome":
                try:
                    copy_recovery_file(
                        Path(fresh.versions_root),
                        fresh.source_path,
                        fresh.destination_path,
                        fresh.content_hash,
                    )
                except RecoveryFileError as exc:
                    raise RecoveryError(exc.code) from exc
                version = _row(conn, "SELECT * FROM document_versions WHERE id=?", (request.version_id,))
                update_version = bool(
                    version is not None
                    and _path_keys(version["file_path"], Path(fresh.versions_root))
                    & _path_keys(fresh.source_path, Path(fresh.versions_root))
                )
            else:
                update_version = True
            # Re-read and re-hash immediately before CAS while the DB writer
            # lock is held. The file layer checks identity before/after reads.
            try:
                verified = verify_recovery_file(Path(fresh.versions_root), fresh.source_path, fresh.content_hash)
                copied = verify_recovery_file(Path(fresh.versions_root), fresh.destination_path, fresh.content_hash)
            except RecoveryFileError as exc:
                raise RecoveryError(exc.code) from exc
            if verified.byte_size != fresh.byte_size or copied.byte_size != fresh.byte_size:
                raise RecoveryError("file_changed")
            cas_recovery_paths(
                conn,
                version_id=request.version_id,
                expected_version_path=fresh.expected_version_path,
                destination_path=fresh.destination_path,
                manifest_id=request.manifest_id,
                expected_manifest_source=fresh.expected_manifest_source,
                update_version=update_version,
            )
            receipt = {
                "schema_version": 1,
                "action": "recovered",
                "operation_key": request.operation_key,
                "kind": request.kind,
                "actor_user_id": request.actor_user_id,
                "document_id": request.document_id,
                "version_id": request.version_id,
                "manifest_id": request.manifest_id,
                "source_path": fresh.source_path,
                "destination_path": fresh.destination_path,
                "content_hash": fresh.content_hash,
                "byte_size": fresh.byte_size,
                "source_preserved": True,
            }
            insert_recovery_receipt(
                conn,
                operation_key=request.operation_key,
                request_hash=fresh.request_hash,
                actor_user_id=request.actor_user_id,
                document_id=request.document_id,
                version_id=request.version_id,
                receipt=receipt,
            )
            return receipt
    except (RecoveryError, KBLifecycleError):
        raise
    except RecoveryReceiptConflict as exc:
        raise RecoveryError("operation_key_conflict") from exc
