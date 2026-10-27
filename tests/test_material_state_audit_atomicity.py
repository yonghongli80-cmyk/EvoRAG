"""RED regressions for atomic organization audits on material state changes."""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from pathlib import Path

from rag.materials.recycle import delete_to_recycle, enqueue_purge_job


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("team/notes.txt", b"synthetic archive member")
    return buffer.getvalue()


def _seed_org_kb(db, *, kb_id: str, admin_id: int) -> None:
    """Create a synthetic org KB with an administrator authorized to mutate it."""
    with db.locked_transaction(immediate=True) as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','Team','team')")
        conn.execute(
            "INSERT INTO kb_metadata(kb_id,name,owner_id) VALUES(?,?,?)",
            (kb_id, "Synthetic KB", admin_id),
        )
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES(?, 'TEAM', 'organization')",
            (kb_id,),
        )


def _reject_org_audit(db, *, trigger: str, action: str) -> None:
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            f"CREATE TRIGGER {trigger} BEFORE INSERT ON org_audit_events "
            f"WHEN NEW.action='{action}' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic audit outage'); END"
        )


def _remove_reject_org_audit(db, trigger: str) -> None:
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(f"DROP TRIGGER {trigger}")


def _rows(db, table: str) -> tuple[tuple, ...]:
    return tuple(tuple(row) for row in db._conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall())


def _archive_state(db) -> dict[str, tuple[tuple, ...]]:
    return {
        table: _rows(db, table)
        for table in (
            "document_permissions",
            "document_versions",
            "material_archive_manifests",
            "material_archive_entries",
            "material_archive_import_results",
            "material_search_fts",
            "document_index_jobs",
            "org_audit_events",
        )
    }


def _file_state(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _seed_current_document(db, *, document_id: int, filename: str, kb_id: str, owner_id: int, root: Path):
    path = root / str(document_id) / "1" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic current material")
    version_id = db.create_document_version(
        document_id,
        filename,
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        owner_id,
    )
    db.activate_document_version(document_id, version_id, kb_id, filename)
    return version_id, path


def test_archive_confirmation_audit_failure_rolls_back_state_and_can_retry(isolated_api, make_user) -> None:
    user, headers = make_user("archive-confirm-audit", is_admin=True)
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    _seed_org_kb(db, kb_id="kb_state_audit", admin_id=user["id"])

    uploaded = isolated_api.post(
        "/materials/upload?kb_id=kb_state_audit",
        headers=headers,
        files={"file": ("bundle.zip", _zip_bytes(), "application/zip")},
    )
    assert uploaded.status_code == 202, uploaded.text
    manifest_id = uploaded.json()["manifest_id"]
    preview = isolated_api.get(f"/materials/archives/{manifest_id}", headers=headers)
    assert preview.status_code == 200, preview.text
    selected_ids = [entry["id"] for entry in preview.json()["entries"] if entry["state"] == "approved"]
    confirm_url = f"/materials/archives/{manifest_id}/confirm"
    body = {
        "selected_entry_ids": selected_ids,
        "expected_version": preview.json()["expected_version"],
        "idempotency_key": "audit-confirm-retry",
    }
    before_sql = _archive_state(db)
    before_files = _file_state(versions_root)
    _reject_org_audit(db, trigger="reject_archive_confirm_audit", action="archive_confirmed")

    failed = isolated_api.post(confirm_url, headers=headers, json=body)

    assert failed.status_code == 500
    assert _archive_state(db) == before_sql
    assert _file_state(versions_root) == before_files
    assert db._conn.in_transaction is False

    _remove_reject_org_audit(db, "reject_archive_confirm_audit")
    retried = isolated_api.post(confirm_url, headers=headers, json=body)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "confirmed"
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='archive_confirmed'").fetchone()[0] == 1


def test_archive_same_key_confirmation_replay_does_not_add_success_audit(isolated_api, make_user) -> None:
    user, headers = make_user("archive-confirm-replay", is_admin=True)
    db = isolated_api.app.state.user_db
    _seed_org_kb(db, kb_id="kb_state_audit", admin_id=user["id"])
    uploaded = isolated_api.post(
        "/materials/upload?kb_id=kb_state_audit",
        headers=headers,
        files={"file": ("replay.zip", _zip_bytes(), "application/zip")},
    )
    assert uploaded.status_code == 202, uploaded.text
    manifest_id = uploaded.json()["manifest_id"]
    preview = isolated_api.get(f"/materials/archives/{manifest_id}", headers=headers).json()
    body = {
        "selected_entry_ids": [entry["id"] for entry in preview["entries"] if entry["state"] == "approved"],
        "expected_version": preview["expected_version"],
        "idempotency_key": "same-confirmation-key",
    }
    url = f"/materials/archives/{manifest_id}/confirm"

    first = isolated_api.post(url, headers=headers, json=body)
    assert first.status_code == 200, first.text
    audit_count_after_first = db._conn.execute(
        "SELECT COUNT(*) FROM org_audit_events WHERE action='archive_confirmed'"
    ).fetchone()[0]

    replay = isolated_api.post(url, headers=headers, json=body)

    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "already_confirmed"
    assert audit_count_after_first == 1
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='archive_confirmed'").fetchone()[0] == 1


def test_recycle_audit_failure_rolls_back_metadata_version_and_jobs_then_retries(isolated_api, make_user) -> None:
    user, headers = make_user("recycle-audit", is_admin=True)
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    _seed_org_kb(db, kb_id="kb_state_audit", admin_id=user["id"])
    document_id = db.create_document_permission("recycle.txt", "kb_state_audit", user["id"])
    version_id, current_path = _seed_current_document(
        db,
        document_id=document_id,
        filename="recycle.txt",
        kb_id="kb_state_audit",
        owner_id=user["id"],
        root=versions_root,
    )
    db.enqueue_material_job(document_id, version_id)
    before_document = tuple(
        db._conn.execute("SELECT * FROM document_permissions WHERE id=?", (document_id,)).fetchone()
    )
    before_versions = tuple(
        tuple(row)
        for row in db._conn.execute(
            "SELECT * FROM document_versions WHERE document_id=? ORDER BY id", (document_id,)
        ).fetchall()
    )
    before_jobs = tuple(
        tuple(row)
        for row in db._conn.execute(
            "SELECT * FROM document_index_jobs WHERE document_id=? ORDER BY id", (document_id,)
        ).fetchall()
    )
    before_audit = _rows(db, "material_audit_log")
    _reject_org_audit(db, trigger="reject_recycle_audit", action="document_recycled")

    failed = isolated_api.delete(f"/materials/{document_id}", headers=headers)

    assert failed.status_code == 500
    assert (
        tuple(db._conn.execute("SELECT * FROM document_permissions WHERE id=?", (document_id,)).fetchone())
        == before_document
    )
    assert (
        tuple(
            tuple(row)
            for row in db._conn.execute(
                "SELECT * FROM document_versions WHERE document_id=? ORDER BY id", (document_id,)
            ).fetchall()
        )
        == before_versions
    )
    assert (
        tuple(
            tuple(row)
            for row in db._conn.execute(
                "SELECT * FROM document_index_jobs WHERE document_id=? ORDER BY id", (document_id,)
            ).fetchall()
        )
        == before_jobs
    )
    assert _rows(db, "material_audit_log") == before_audit
    assert current_path.read_bytes() == b"synthetic current material"
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='document_recycled'").fetchone()[0] == 0
    assert db._conn.in_transaction is False

    _remove_reject_org_audit(db, "reject_recycle_audit")
    retried = isolated_api.delete(f"/materials/{document_id}", headers=headers)
    assert retried.status_code == 200, retried.text
    assert db.get_document_permission_by_id(document_id)["deleted_at"] is not None
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='document_recycled'").fetchone()[0] == 1


def test_restore_audit_failure_rolls_back_metadata_version_and_purge_job_then_retries(isolated_api, make_user) -> None:
    user, headers = make_user("restore-audit", is_admin=True)
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    _seed_org_kb(db, kb_id="kb_state_audit", admin_id=user["id"])
    document_id = db.create_document_permission("restore.txt", "kb_state_audit", user["id"])
    version_id, current_path = _seed_current_document(
        db,
        document_id=document_id,
        filename="restore.txt",
        kb_id="kb_state_audit",
        owner_id=user["id"],
        root=versions_root,
    )
    db.enqueue_material_job(document_id, version_id)
    deleted = delete_to_recycle(db, document_id, user, now=time.time(), organization_authorized=True)
    purge_job_id = enqueue_purge_job(db, document_id, now=time.time())
    assert purge_job_id is not None
    before_document = tuple(
        db._conn.execute("SELECT * FROM document_permissions WHERE id=?", (document_id,)).fetchone()
    )
    before_versions = tuple(
        tuple(row)
        for row in db._conn.execute(
            "SELECT * FROM document_versions WHERE document_id=? ORDER BY id", (document_id,)
        ).fetchall()
    )
    before_jobs = tuple(
        tuple(row)
        for row in db._conn.execute(
            "SELECT * FROM document_index_jobs WHERE document_id=? ORDER BY id", (document_id,)
        ).fetchall()
    )
    before_audit = _rows(db, "material_audit_log")
    _reject_org_audit(db, trigger="reject_restore_audit", action="document_restored")

    failed = isolated_api.post(
        f"/materials/{document_id}/restore",
        headers=headers,
        json={"expected_metadata_version": deleted.metadata_version},
    )

    assert failed.status_code == 500
    assert (
        tuple(db._conn.execute("SELECT * FROM document_permissions WHERE id=?", (document_id,)).fetchone())
        == before_document
    )
    assert (
        tuple(
            tuple(row)
            for row in db._conn.execute(
                "SELECT * FROM document_versions WHERE document_id=? ORDER BY id", (document_id,)
            ).fetchall()
        )
        == before_versions
    )
    assert (
        tuple(
            tuple(row)
            for row in db._conn.execute(
                "SELECT * FROM document_index_jobs WHERE document_id=? ORDER BY id", (document_id,)
            ).fetchall()
        )
        == before_jobs
    )
    assert _rows(db, "material_audit_log") == before_audit
    assert current_path.read_bytes() == b"synthetic current material"
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='document_restored'").fetchone()[0] == 0
    assert db._conn.in_transaction is False

    _remove_reject_org_audit(db, "reject_restore_audit")
    retried = isolated_api.post(
        f"/materials/{document_id}/restore",
        headers=headers,
        json={"expected_metadata_version": deleted.metadata_version},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "restored"
    assert db.get_document_permission_by_id(document_id)["deleted_at"] is None
    assert (
        db._conn.execute("SELECT status FROM document_index_jobs WHERE id=?", (purge_job_id,)).fetchone()[0]
        == "cancelled"
    )
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='document_restored'").fetchone()[0] == 1
