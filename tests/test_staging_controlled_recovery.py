"""Synthetic-only tests for the explicit staging recovery maintenance API."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from rag.kb_lifecycle import KBLifecycleError
from rag.materials.recovery import (
    RecoveryError,
    RecoveryRequest,
    _plan_hash,
    apply_staging_recovery,
    preview_staging_recovery,
)
from rag.user_db import UserDB


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prepare(tmp_path: Path, kind: str = "version_pointer", *, relative_archive_source: bool = False):
    db = UserDB(tmp_path / "users.db")
    db.create_material_schema()
    user_id = db.create_user("synthetic-recovery", "Password-123!")
    db.create_kb_metadata("kb-recovery", "Synthetic recovery", user_id)
    document_id = db.create_document_permission("synthetic.txt", "kb-recovery", user_id, 1, False)
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('RECOVERY-TEST',?,'Synthetic user','Tester')",
            (user_id,),
        )
    root = tmp_path / "versions"
    root.mkdir()
    version_dir = root / str(document_id) / "1"
    version_dir.mkdir(parents=True)
    body = b"synthetic recovery payload\x00"
    candidate = version_dir / "candidate.bin"
    candidate.write_bytes(body)
    missing_path = root / "staging" / "missing.bin"
    version_id = db.create_document_version(document_id, "synthetic.txt", str(missing_path), _digest(body), user_id)
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE document_versions SET status='staging',version_no=1,file_path=? WHERE id=?",
            (str(missing_path), version_id),
        )
    manifest_id = None
    if kind == "archive_rehome":
        db.create_kb_metadata("kb-recovery", "Synthetic recovery", user_id)
        source = root / "staging" / "legacy-archive.bin"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(body)
        candidate = source
        with db.locked_transaction() as conn:
            source_value = "staging/legacy-archive.bin" if relative_archive_source else str(source)
            version_value = source_value if relative_archive_source else str(source)
            conn.execute(
                "UPDATE document_versions SET file_path=? WHERE id=?",
                (version_value, version_id),
            )
            cursor = conn.execute(
                "INSERT INTO material_archive_manifests "
                "(container_document_id,container_version_id,status,created_by,source_path,expires_at) "
                "VALUES(?,?,'pending_confirmation',?,?,?)",
                (document_id, version_id, user_id, source_value, time.time() + 3600),
            )
            manifest_id = int(cursor.lastrowid)
    request = RecoveryRequest(
        operation_key="synthetic-recovery-1",
        kind=kind,
        actor_user_id=user_id,
        document_id=document_id,
        version_id=version_id,
        candidate_path=str(candidate) if kind == "version_pointer" else "",
        manifest_id=manifest_id,
    )
    return db, root, body, candidate, missing_path, request


def _snapshot(root: Path) -> tuple[tuple[str, bytes | str], ...]:
    items = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            items.append((path.relative_to(root).as_posix(), path.read_bytes()))
        elif path.is_dir():
            items.append((path.relative_to(root).as_posix(), "directory"))
    return tuple(items)


def test_version_pointer_preview_is_read_only_then_apply_preserves_candidate(tmp_path: Path):
    db, root, body, candidate, missing, request = _prepare(tmp_path)
    try:
        tree_before = _snapshot(root)
        receipt_table_before = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='material_recovery_receipts'"
        ).fetchone()
        plan = preview_staging_recovery(db, root, request)
        assert _snapshot(root) == tree_before
        assert db.get_document_version_by_id(request.version_id)["file_path"] == str(missing)
        assert (
            db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='material_recovery_receipts'"
            ).fetchone()
            == receipt_table_before
        )

        receipt = apply_staging_recovery(db, plan, maintenance_confirmed=True)
        assert receipt["schema_version"] == 1
        assert receipt["action"] == "recovered"
        assert receipt["source_preserved"] is True
        assert Path(receipt["source_path"]).read_bytes() == body
        assert db.get_document_version_by_id(request.version_id)["file_path"] == str(candidate)
        assert Path(receipt["destination_path"]).read_bytes() == body
    finally:
        db.close()


def test_archive_rehome_is_copy_only_and_same_key_replay_is_identical(tmp_path: Path):
    db, root, body, source, _missing, request = _prepare(tmp_path, "archive_rehome")
    try:
        plan = preview_staging_recovery(db, root, request)
        receipt = apply_staging_recovery(db, plan, maintenance_confirmed=True)
        assert source.read_bytes() == body
        assert Path(receipt["destination_path"]).read_bytes() == body
        replay = apply_staging_recovery(db, plan, maintenance_confirmed=True)
        assert replay == receipt
        manifest = db._conn.execute(
            "SELECT status,source_path FROM material_archive_manifests WHERE id=?",
            (request.manifest_id,),
        ).fetchone()
        assert tuple(manifest) == ("pending_confirmation", receipt["destination_path"])
    finally:
        db.close()


def test_archive_rehome_normalizes_relative_version_path_and_keeps_manifest_cas_value(tmp_path: Path):
    db, root, body, source, _missing, request = _prepare(tmp_path, "archive_rehome", relative_archive_source=True)
    try:
        plan = preview_staging_recovery(db, root, request)
        receipt = apply_staging_recovery(db, plan, maintenance_confirmed=True)
        version_path = db.get_document_version_by_id(request.version_id)["file_path"]
        assert version_path == receipt["destination_path"]
        assert Path(receipt["source_path"]).read_bytes() == body
        manifest = db._conn.execute(
            "SELECT source_path FROM material_archive_manifests WHERE id=?",
            (request.manifest_id,),
        ).fetchone()
        assert manifest[0] == receipt["destination_path"]
        assert source.read_bytes() == body
    finally:
        db.close()


def test_same_operation_key_with_different_request_conflicts(tmp_path: Path):
    db, root, _body, candidate, _missing, request = _prepare(tmp_path)
    try:
        plan = preview_staging_recovery(db, root, request)
        apply_staging_recovery(db, plan, maintenance_confirmed=True)
        other_candidate = candidate.with_name("other.bin")
        other_candidate.write_bytes(candidate.read_bytes())
        different = RecoveryRequest(**{**request.__dict__, "candidate_path": str(other_candidate)})
        forged = replace(
            plan,
            request=different,
            source_path=str(other_candidate),
            destination_path=str(other_candidate),
            request_hash="",
        )
        forged = replace(forged, request_hash=_plan_hash(forged))
        with pytest.raises(RecoveryError, match="operation_key_conflict"):
            apply_staging_recovery(db, forged, maintenance_confirmed=True)
    finally:
        db.close()


def test_apply_rejects_stale_or_tampered_plan(tmp_path: Path):
    db, root, _body, _candidate, _missing, request = _prepare(tmp_path)
    try:
        plan = preview_staging_recovery(db, root, request)
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE document_permissions SET owner_id=owner_id+1 WHERE id=?",
                (request.document_id,),
            )
        with pytest.raises(RecoveryError):
            apply_staging_recovery(db, plan, maintenance_confirmed=True)
    finally:
        db.close()


@pytest.mark.parametrize("bad_kind", ["hash", "current", "job", "permission"])
def test_preview_fails_closed_on_untrusted_or_shared_state(tmp_path: Path, bad_kind: str):
    db, root, body, candidate, _missing, request = _prepare(tmp_path)
    try:
        with db.locked_transaction() as conn:
            if bad_kind == "hash":
                conn.execute(
                    "UPDATE document_versions SET content_hash=? WHERE id=?", (_digest(b"wrong"), request.version_id)
                )
            elif bad_kind == "current":
                conn.execute(
                    "UPDATE document_permissions SET current_version_id=? WHERE id=?",
                    (request.version_id, request.document_id),
                )
            elif bad_kind == "job":
                conn.execute(
                    "INSERT INTO document_index_jobs(document_id,version_id,content_hash,status,available_at) "
                    "VALUES(?,?,?,'retry',?)",
                    (request.document_id, request.version_id, _digest(body), time.time()),
                )
            else:
                conn.execute("UPDATE document_permissions SET owner_id=owner_id+1 WHERE id=?", (request.document_id,))
        with pytest.raises(RecoveryError):
            preview_staging_recovery(db, root, request)
    finally:
        db.close()


def test_inactive_actor_is_rejected(tmp_path: Path):
    db, root, _body, _candidate, _missing, request = _prepare(tmp_path)
    try:
        with db.locked_transaction() as conn:
            conn.execute("UPDATE org_people SET active=0 WHERE user_id=?", (request.actor_user_id,))
        with pytest.raises(RecoveryError, match="actor_inactive"):
            preview_staging_recovery(db, root, request)
    finally:
        db.close()


def test_denied_kb_audit_is_rolled_back_with_preview(tmp_path: Path):
    db, root, _body, _candidate, _missing, request = _prepare(tmp_path)
    try:
        denied_user = db.create_user("synthetic-denied", "Password-123!")
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
                "VALUES('DENIED-TEST',?,'Denied user','Tester')",
                (denied_user,),
            )
            conn.execute("INSERT INTO kb_org_policies(kb_id,kb_type,active) VALUES('kb-recovery','unassigned',1)")
        before = db._conn.execute("SELECT COUNT(*) FROM org_audit_events").fetchone()[0]
        denied_request = replace(request, actor_user_id=denied_user)
        with pytest.raises(RecoveryError, match="authorization_denied"):
            preview_staging_recovery(db, root, denied_request)
        after = db._conn.execute("SELECT COUNT(*) FROM org_audit_events").fetchone()[0]
        assert after == before
    finally:
        db.close()


def test_missing_actor_is_rejected(tmp_path: Path):
    db, root, _body, _candidate, _missing, request = _prepare(tmp_path)
    try:
        missing_actor = replace(request, actor_user_id=request.actor_user_id + 10000)
        with pytest.raises(RecoveryError, match="actor_missing"):
            preview_staging_recovery(db, root, missing_actor)
    finally:
        db.close()


def test_archive_rejects_expired_manifest_and_deleted_kb(tmp_path: Path):
    db, root, _body, _source, _missing, request = _prepare(tmp_path, "archive_rehome")
    try:
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE material_archive_manifests SET expires_at=? WHERE id=?",
                (time.time() - 1, request.manifest_id),
            )
        with pytest.raises(RecoveryError, match="manifest_expired"):
            preview_staging_recovery(db, root, request)
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE material_archive_manifests SET expires_at=? WHERE id=?",
                (time.time() + 3600, request.manifest_id),
            )
            conn.execute(
                "INSERT INTO kb_lifecycle(kb_id,state,generation,requested_at,deleted_at) "
                "VALUES('kb-recovery','deleted',1,?,?)",
                (time.time(), time.time()),
            )
        with pytest.raises(KBLifecycleError) as exc_info:
            preview_staging_recovery(db, root, request)
        assert exc_info.value.code == "knowledge_base_deleted"
    finally:
        db.close()


def test_archive_rejects_confirmed_manifest_and_rag_docs_target(tmp_path: Path):
    db, root, _body, _source, _missing, request = _prepare(tmp_path, "archive_rehome")
    try:
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE material_archive_manifests SET status='confirmed' WHERE id=?",
                (request.manifest_id,),
            )
        with pytest.raises(RecoveryError, match="manifest_not_pending"):
            preview_staging_recovery(db, root, request)
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE material_archive_manifests SET status='pending_confirmation' WHERE id=?",
                (request.manifest_id,),
            )
            conn.execute(
                "UPDATE document_permissions SET kb_id='rag_docs' WHERE id=?",
                (request.document_id,),
            )
        with pytest.raises(RecoveryError, match="rag_docs_archive_rehome_forbidden"):
            preview_staging_recovery(db, root, request)
    finally:
        db.close()


def test_replay_rechecks_current_acl_before_returning_receipt(tmp_path: Path):
    db, root, _body, _candidate, _missing, request = _prepare(tmp_path)
    try:
        plan = preview_staging_recovery(db, root, request)
        apply_staging_recovery(db, plan, maintenance_confirmed=True)
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE document_permissions SET owner_id=owner_id+1 WHERE id=?",
                (request.document_id,),
            )
        with pytest.raises(RecoveryError, match="authorization_denied"):
            apply_staging_recovery(db, plan, maintenance_confirmed=True)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("kind", "change"),
    [
        ("version_pointer", "owner_and_kb_still_authorized"),
        ("archive_rehome", "owner_and_kb_still_authorized"),
        ("archive_rehome", "manifest_container"),
        ("archive_rehome", "manifest_status"),
        ("archive_rehome", "manifest_expiry"),
        ("archive_rehome", "new_version_reference"),
        ("archive_rehome", "new_manifest_reference"),
        ("archive_rehome", "new_kb_document_reference"),
        ("archive_rehome", "new_active_job"),
    ],
)
def test_replay_rejects_changed_post_state(tmp_path: Path, kind: str, change: str):
    db, root, body, source, _missing, request = _prepare(tmp_path, kind)
    try:
        plan = preview_staging_recovery(db, root, request)
        public_receipt = apply_staging_recovery(db, plan, maintenance_confirmed=True)
        other_user = db.create_user("synthetic-new-owner", "Password-123!")
        with db.locked_transaction() as conn:
            if change == "owner_and_kb_still_authorized":
                db.set_user_admin(request.actor_user_id, True)
                db.create_kb_metadata("kb-other", "Other synthetic KB", other_user)
                conn.execute(
                    "UPDATE document_permissions SET owner_id=?,kb_id='kb-other' WHERE id=?",
                    (other_user, request.document_id),
                )
            elif change == "manifest_container":
                conn.execute(
                    "UPDATE material_archive_manifests SET container_version_id=container_version_id+100 WHERE id=?",
                    (request.manifest_id,),
                )
            elif change == "manifest_status":
                conn.execute(
                    "UPDATE material_archive_manifests SET status='confirmed' WHERE id=?",
                    (request.manifest_id,),
                )
            elif change == "manifest_expiry":
                conn.execute(
                    "UPDATE material_archive_manifests SET expires_at=? WHERE id=?",
                    (time.time() - 1, request.manifest_id),
                )
            elif change == "new_version_reference":
                other_doc = db.create_document_permission(
                    "new-reference.txt", "kb-recovery", request.actor_user_id, 1, False
                )
                other_version = db.create_document_version(
                    other_doc,
                    "new-reference.txt",
                    str(source),
                    _digest(body),
                    request.actor_user_id,
                )
                conn.execute(
                    "UPDATE document_versions SET status='staging',file_path=? WHERE id=?",
                    (public_receipt["destination_path"], other_version),
                )
            elif change == "new_manifest_reference":
                conn.execute(
                    "INSERT INTO material_archive_manifests "
                    "(container_document_id,container_version_id,status,created_by,source_path,expires_at) "
                    "VALUES(?,?,'pending_confirmation',?,?,?)",
                    (
                        request.document_id,
                        request.version_id,
                        request.actor_user_id,
                        public_receipt["destination_path"],
                        time.time() + 3600,
                    ),
                )
            elif change == "new_kb_document_reference":
                conn.execute(
                    "INSERT INTO kb_documents(kb_id,filename,file_path,document_id,document_version_id) "
                    "VALUES('kb-recovery','new-reference.txt',?,?,?)",
                    (public_receipt["destination_path"], request.document_id, request.version_id),
                )
            elif change == "new_active_job":
                conn.execute(
                    "INSERT INTO document_index_jobs(document_id,version_id,content_hash,status,available_at) "
                    "VALUES(?,?,?,'running',?)",
                    (request.document_id, request.version_id, _digest(body), time.time()),
                )
        with pytest.raises(RecoveryError):
            replay = apply_staging_recovery(db, plan, maintenance_confirmed=True)
            assert replay == public_receipt
    finally:
        db.close()


def test_archive_rejects_same_path_referenced_by_another_kb_version(tmp_path: Path):
    db, root, _body, source, _missing, request = _prepare(tmp_path, "archive_rehome")
    try:
        other_doc = db.create_document_permission("other.txt", "kb-other", request.actor_user_id, 1, False)
        other_version = db.create_document_version(
            other_doc, "other.txt", str(source), _digest(source.read_bytes()), request.actor_user_id
        )
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE document_versions SET file_path=?,status='staging' WHERE id=?",
                ("staging/legacy-archive.bin", other_version),
            )
        with pytest.raises(RecoveryError, match="shared_reference"):
            preview_staging_recovery(db, root, request)
    finally:
        db.close()


def test_nested_writer_transaction_is_rejected(tmp_path: Path):
    db, root, _body, _candidate, _missing, request = _prepare(tmp_path)
    try:
        with db.transaction(), pytest.raises(RecoveryError, match="nested_transaction"):
            preview_staging_recovery(db, root, request)
    finally:
        db.close()


def test_apply_requires_confirmation_and_rejects_nested_transaction(tmp_path: Path):
    db, root, _body, _candidate, _missing, request = _prepare(tmp_path)
    try:
        plan = preview_staging_recovery(db, root, request)
        with pytest.raises(RecoveryError, match="maintenance_confirmation_required"):
            apply_staging_recovery(db, plan)
        with db.transaction(), pytest.raises(RecoveryError, match="nested_transaction"):
            apply_staging_recovery(db, plan, maintenance_confirmed=True)
    finally:
        db.close()


def test_archive_commit_failure_keeps_both_source_and_copy_for_same_request_retry(tmp_path: Path):
    db, root, body, source, _missing, request = _prepare(tmp_path, "archive_rehome")
    try:
        plan = preview_staging_recovery(db, root, request)
        with db.locked_transaction() as conn:
            conn.execute("CREATE TABLE recovery_parent (id INTEGER PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE recovery_deferred_child (parent_id INTEGER NOT NULL, "
                "FOREIGN KEY(parent_id) REFERENCES recovery_parent(id) DEFERRABLE INITIALLY DEFERRED)"
            )
            conn.execute(
                "CREATE TRIGGER fail_recovery_commit AFTER UPDATE ON document_versions "
                f"WHEN OLD.id={request.version_id} BEGIN "
                "INSERT INTO recovery_deferred_child(parent_id) VALUES(999); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            apply_staging_recovery(db, plan, maintenance_confirmed=True)
        assert source.read_bytes() == body
        assert Path(plan.destination_path).read_bytes() == body
        assert db.get_document_version_by_id(request.version_id)["file_path"] == str(source)
        with db.locked_transaction() as conn:
            conn.execute("DROP TRIGGER fail_recovery_commit")
        receipt = apply_staging_recovery(db, plan, maintenance_confirmed=True)
        assert receipt["source_preserved"] is True
    finally:
        db.close()
