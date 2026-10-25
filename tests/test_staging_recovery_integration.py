"""Scoped regressions for compensation transactions and staging recovery audit."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from rag.staging_audit import audit_staging
from rag.user_db import UserDB


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(root: Path) -> tuple[tuple[str, str], ...]:
    snapshot: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot.append((relative, f"link:{os.readlink(path)}"))
        elif path.is_file():
            snapshot.append((relative, _file_digest(path)))
        elif path.is_dir():
            snapshot.append((relative, "directory"))
    return tuple(snapshot)


@pytest.mark.parametrize("delete_kind", ["version", "permission"])
def test_delete_commit_failure_rolls_back_shared_writer_and_allows_followup_write(
    tmp_path: Path, delete_kind: str
) -> None:
    db = UserDB(tmp_path / "users.db")
    try:
        db.create_material_schema()
        user_id = db.create_user(f"delete-{delete_kind}", "Password-123!")
        document_id = db.create_document_permission(
            "delete-target.txt", "kb-delete", user_id, 1, False
        )
        if delete_kind == "version":
            target_id = db.create_document_version(
                document_id,
                "delete-target.txt",
                str(tmp_path / "unused-version.txt"),
                "synthetic-hash",
                user_id,
            )
            target_table = "document_versions"
        else:
            target_id = document_id
            target_table = "document_permissions"

        with db.locked_transaction(immediate=True) as conn:
            conn.execute("CREATE TABLE deferred_delete_parent (id INTEGER PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE deferred_delete_child (parent_id INTEGER NOT NULL, "
                "FOREIGN KEY(parent_id) REFERENCES deferred_delete_parent(id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )
            conn.execute(
                f"CREATE TRIGGER fail_{delete_kind}_delete_commit "
                f"AFTER DELETE ON {target_table} WHEN OLD.id={target_id} "
                "BEGIN INSERT INTO deferred_delete_child(parent_id) VALUES (999); END"
            )

        delete = (
            db.delete_document_version
            if delete_kind == "version"
            else db.delete_document_permission
        )
        with pytest.raises(sqlite3.IntegrityError):
            delete(target_id)

        assert db._conn.in_transaction is False
        assert db._conn.execute(
            f"SELECT id FROM {target_table} WHERE id=?", (target_id,)
        ).fetchone() is not None
        assert db._conn.execute(
            "SELECT COUNT(*) FROM deferred_delete_child"
        ).fetchone()[0] == 0

        db.create_kb_metadata("kb-after-delete-failure", "After delete failure", user_id)
        assert db._conn.in_transaction is False
        assert db.get_kb_metadata("kb-after-delete-failure") is not None
    finally:
        db.close()


@pytest.mark.parametrize("delete_kind", ["version", "permission"])
def test_upload_compensation_delete_commit_failure_isolated_route(
    isolated_api, make_user, delete_kind: str
) -> None:
    user, headers = make_user(f"upload-delete-{delete_kind}")
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    filename = f"compensation-{delete_kind}.txt"
    body = f"synthetic {delete_kind} compensation bytes".encode()

    old_version_id = None
    document_id = None
    if delete_kind == "version":
        document_id = db.create_document_permission(filename, "rag_docs", user["id"], 1, False)
        old_path = versions_root / str(document_id) / "1" / filename
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"prior current version")
        old_version_id = db.create_document_version(
            document_id, filename, str(old_path), "prior-hash", user["id"]
        )
        db.set_version_file_path(old_version_id, str(old_path))
        db.activate_document_version(document_id, old_version_id, "rag_docs", filename)

    with db.locked_transaction(immediate=True) as conn:
        conn.execute("CREATE TABLE deferred_cleanup_parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE deferred_cleanup_child (parent_id INTEGER NOT NULL, "
            "FOREIGN KEY(parent_id) REFERENCES deferred_cleanup_parent(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )
        if delete_kind == "version":
            conn.execute(
                "CREATE TRIGGER fail_upload_version_delete_commit "
                "AFTER DELETE ON document_versions "
                f"WHEN OLD.document_id={document_id} AND OLD.id!={old_version_id} "
                "BEGIN INSERT INTO deferred_cleanup_child(parent_id) VALUES (999); END"
            )
        else:
            conn.execute(
                "CREATE TRIGGER fail_upload_permission_delete_commit "
                "AFTER DELETE ON document_permissions "
                "BEGIN INSERT INTO deferred_cleanup_child(parent_id) VALUES (999); END"
            )
        conn.execute(
            "CREATE TRIGGER reject_material_upload_job "
            "BEFORE INSERT ON document_index_jobs "
            "WHEN NEW.job_scope='material' AND NEW.job_type='material_index' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic enqueue failure'); END"
        )

    response = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": (filename, body, "text/plain")},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "material_promotion_failed"
    assert db._conn.in_transaction is False
    permission = db.get_document_permission(filename, "rag_docs")
    assert permission is not None
    assert permission["owner_id"] == user["id"]
    versions = db.list_document_versions(permission["id"])
    if delete_kind == "version":
        assert len(versions) == 2
        prior = next(version for version in versions if version["id"] == old_version_id)
        compensation = next(version for version in versions if version["id"] != old_version_id)
        assert prior["status"] == "current"
        assert Path(prior["file_path"]).read_bytes() == b"prior current version"
        assert compensation["status"] == "staging"
        compensation_stage = Path(compensation["file_path"])
        assert compensation_stage.is_relative_to(versions_root / "staging")
        assert compensation_stage.read_bytes() == body
        assert db.get_current_document_version(permission["id"])["id"] == old_version_id
    else:
        assert versions == []
        staged_files = list((versions_root / "staging").glob(f"*-{filename}"))
        assert len(staged_files) == 1
        assert staged_files[0].read_bytes() == body

    db.create_kb_metadata(f"after-{delete_kind}-delete-failure", "After cleanup failure", user["id"])
    assert db._conn.in_transaction is False


def test_rag_docs_route_snapshot_reports_candidate_after_moveback_and_repoint_failures(
    isolated_api, make_user, tmp_path: Path, monkeypatch, caplog
) -> None:
    import rag.document_versions as document_versions

    user, headers = make_user("rag-docs-recovery")
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    filename = "recovery-candidate.txt"
    body = b"synthetic bytes retained after failed compensation"

    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "CREATE TRIGGER reject_material_upload_job "
            "BEFORE INSERT ON document_index_jobs "
            "WHEN NEW.job_scope='material' AND NEW.job_type='material_index' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic enqueue failure'); END"
        )

    real_promote = document_versions.promote_version
    staging_root = versions_root / "staging"

    def fail_move_back(source: Path, destination: Path) -> None:
        destination_path = Path(destination)
        if destination_path.is_relative_to(staging_root):
            raise OSError("synthetic move-back failure")
        real_promote(source, destination)

    monkeypatch.setattr(document_versions, "promote_version", fail_move_back)

    update_attempts = 0

    def fail_recovery_repoint(action, table, column, database_name, source):
        nonlocal update_attempts
        if (
            action == sqlite3.SQLITE_UPDATE
            and table == "document_versions"
            and column == "file_path"
        ):
            update_attempts += 1
            if update_attempts == 2:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db._conn.set_authorizer(fail_recovery_repoint)
    try:
        with caplog.at_level(logging.ERROR, logger="rag.document_versions"):
            response = isolated_api.post(
                "/materials/upload",
                headers=headers,
                files={"file": (filename, body, "text/plain")},
            )
    finally:
        db._conn.set_authorizer(None)

    assert response.status_code == 500
    assert response.json()["detail"] == "material_promotion_failed"
    assert update_attempts == 2
    assert "could not record staged upload recovery path" in caplog.text
    assert db._conn.in_transaction is False

    permission = db.get_document_permission(filename, "rag_docs")
    assert permission is not None
    assert permission["owner_id"] == user["id"]
    versions = db.list_document_versions(permission["id"])
    assert len(versions) == 1
    version = versions[0]
    assert version["status"] == "staging"
    declared_path = Path(version["file_path"])
    assert declared_path.is_relative_to(staging_root)
    assert not declared_path.exists()

    final_path = versions_root / str(permission["id"]) / str(version["version_no"]) / filename
    assert final_path.read_bytes() == body

    snapshot_path = tmp_path / "post-route-snapshot.db"
    snapshot_conn = sqlite3.connect(snapshot_path)
    try:
        db._conn.backup(snapshot_conn)
    finally:
        snapshot_conn.close()

    before_snapshot = _file_digest(snapshot_path)
    before_tree = _tree_snapshot(versions_root)
    before_permission = db.get_document_permission_by_id(permission["id"])
    before_versions = db.list_document_versions(permission["id"])

    report = audit_staging(snapshot_path, versions_root)

    candidates = [
        entry
        for entry in report["entries"]
        if entry.get("kind") == "owned_version_candidate"
    ]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["path"] == str(final_path)
    assert candidate["candidate_only"] is True
    assert candidate["declared_path_missing"] is True
    assert candidate["action"] == "inspect_only"
    assert "original_filename" not in candidate
    assert "content_hash" not in candidate
    assert _file_digest(snapshot_path) == before_snapshot
    assert _tree_snapshot(versions_root) == before_tree
    assert db.get_document_permission_by_id(permission["id"]) == before_permission
    assert db.list_document_versions(permission["id"]) == before_versions
