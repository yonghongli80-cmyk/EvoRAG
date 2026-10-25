"""Atomic material upload promotion and conservative compensation tests."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from rag.document_versions import VersionPromotionError, promote_staged_version
from rag.user_db import UserDB


@pytest.fixture
def material_db(tmp_path):
    db = UserDB(tmp_path / "users.db")
    user_id = db.create_user("alice", "Password-123!")
    db.create_kb_metadata("kb-1", "Material test KB", owner_id=user_id)
    yield db, user_id
    db.close()


def _job_rows(db: UserDB, document_id: int) -> list[dict]:
    with db.cm.read_connection() as conn:
        rows = conn.execute(
            "SELECT id, version_id, status, lease_owner, lease_expires_at "
            "FROM document_index_jobs WHERE document_id=? ORDER BY id",
            (document_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _seed_current_document(db: UserDB, user_id: int, versions_root: Path, filename="report.txt", kb_id="kb-1"):
    document_id = db.create_document_permission(filename, kb_id, user_id, 1, False)
    old_path = versions_root / str(document_id) / "1" / filename
    old_path.parent.mkdir(parents=True)
    old_bytes = b"old current material"
    old_path.write_bytes(old_bytes)
    old_version_id = db.create_document_version(
        document_id, filename, str(old_path), hashlib.sha256(old_bytes).hexdigest(), user_id
    )
    db.set_version_file_path(old_version_id, str(old_path))
    db.activate_document_version(document_id, old_version_id, kb_id, filename)
    old_job_ids = [
        db.enqueue_material_job(document_id, old_version_id),
        db.enqueue_material_job(document_id, old_version_id),
    ]
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE document_index_jobs SET status='queued' WHERE id=?",
            (old_job_ids[0],),
        )
        conn.execute(
            "UPDATE document_index_jobs SET status='running', lease_owner='worker-a', "
            "lease_expires_at=12345 WHERE id=?",
            (old_job_ids[1],),
        )
    return document_id, old_version_id, old_path, old_job_ids


@pytest.mark.parametrize("seed_existing", [False, True])
def test_sqlite_job_insert_failure_rolls_back_new_and_existing_uploads(
    isolated_api, make_user, seed_existing
):
    """A failed enqueue must leave no jobless version and preserve the old current state."""
    user, headers = make_user("upload-commit")
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    filename = "report.txt" if seed_existing else "new-report.txt"

    old_job_ids = []
    old_version_id = None
    old_path = None
    if seed_existing:
        document_id, old_version_id, old_path, old_job_ids = _seed_current_document(
            db, user["id"], versions_root, filename, kb_id="rag_docs"
        )
        old_state = _job_rows(db, document_id)

    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "CREATE TRIGGER reject_material_upload_job "
            "BEFORE INSERT ON document_index_jobs "
            "WHEN NEW.job_scope='material' AND NEW.job_type='material_index' "
            "BEGIN SELECT RAISE(ABORT, 'injected job enqueue failure'); END"
        )

    response = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": (filename, b"new upload bytes", "text/plain")},
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "material_promotion_failed"
    staged_files = list((versions_root / "staging").glob(f"*-{filename}"))
    assert len(staged_files) == 1
    assert staged_files[0].read_bytes() == b"new upload bytes"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM document_index_jobs"
    ).fetchone()[0] == len(old_job_ids)

    if seed_existing:
        assert db.get_current_document_version(document_id)["id"] == old_version_id
        assert [v["id"] for v in db.list_document_versions(document_id)] == [
            old_version_id
        ]
        assert _job_rows(db, document_id) == old_state
        assert old_path.is_file()
        assert old_path.read_bytes() == b"old current material"
        assert not (versions_root / str(document_id) / "2" / filename).exists()
    else:
        assert db.get_document_permission(filename, "rag_docs") is None
        assert db._conn.execute(
            "SELECT COUNT(*) FROM document_versions"
        ).fetchone()[0] == 0

    with db.locked_transaction(immediate=True) as conn:
        conn.execute("DROP TRIGGER reject_material_upload_job")

def test_finalize_callback_uses_one_transaction_and_rolls_back_when_it_raises(
    tmp_path, material_db
):
    db, user_id = material_db
    versions_root = tmp_path / "versions"
    document_id, old_version_id, old_path, _ = _seed_current_document(
        db, user_id, versions_root
    )
    old_jobs_before = _job_rows(db, document_id)
    staged = versions_root / "staging" / "incoming-report.txt"
    staged.parent.mkdir(parents=True)
    body = b"new callback upload"
    staged.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    callback_ids = []

    def finalize(promoted):
        callback_ids.append(promoted.version_id)
        assert db.finalize_material_upload(
            document_id,
            promoted.version_id,
            staged,
            promoted.file_path,
            digest,
            456.0,
        ) > 0
        raise RuntimeError("force the enclosing transaction to roll back")

    with pytest.raises(VersionPromotionError) as caught:
        promote_staged_version(
            db,
            versions_root,
            document_id=document_id,
            filename="report.txt",
            staged_path=staged,
            content_hash=digest,
            uploaded_by=user_id,
            finalize=finalize,
        )

    assert caught.value.recovery_required is False
    assert len(callback_ids) == 1
    assert staged.read_bytes() == body
    assert not (versions_root / str(document_id) / "2" / "report.txt").exists()
    assert [v["id"] for v in db.list_document_versions(document_id)] == [old_version_id]
    assert db.get_current_document_version(document_id)["id"] == old_version_id
    assert _job_rows(db, document_id) == old_jobs_before
    assert old_path.read_bytes() == b"old current material"


def test_move_that_completed_before_raising_is_moved_back_and_reservation_removed(
    tmp_path, material_db, monkeypatch
):
    import rag.document_versions as document_versions

    db, user_id = material_db
    versions_root = tmp_path / "versions"
    document_id = db.create_document_permission("move.txt", "kb-1", user_id, 1, False)
    staged = versions_root / "staging" / "incoming-move.txt"
    staged.parent.mkdir(parents=True)
    body = b"move succeeded before reporting failure"
    staged.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    real_move = document_versions.promote_version
    calls = 0

    def move_then_raise(source, destination):
        nonlocal calls
        calls += 1
        real_move(source, destination)
        if calls == 1:
            raise OSError("move took effect before the error")

    monkeypatch.setattr(document_versions, "promote_version", move_then_raise)

    with pytest.raises(VersionPromotionError) as caught:
        promote_staged_version(
            db,
            versions_root,
            document_id=document_id,
            filename="move.txt",
            staged_path=staged,
            content_hash=digest,
            uploaded_by=user_id,
        )

    final = versions_root / str(document_id) / "1" / "move.txt"
    assert caught.value.recovery_required is False
    assert calls == 2
    assert staged.read_bytes() == body
    assert not final.exists()
    assert db.list_document_versions(document_id) == []


def test_move_back_that_raises_after_restoring_bytes_is_treated_as_compensated(
    tmp_path, material_db, monkeypatch
):
    import rag.document_versions as document_versions

    db, user_id = material_db
    versions_root = tmp_path / "versions"
    document_id = db.create_document_permission("restore.txt", "kb-1", user_id, 1, False)
    staged = versions_root / "staging" / "incoming-restore.txt"
    staged.parent.mkdir(parents=True)
    body = b"move-back side effect completed"
    staged.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    real_move = document_versions.promote_version
    calls = 0

    def move_and_report_failure(source, destination):
        nonlocal calls
        calls += 1
        real_move(source, destination)
        raise OSError("reported after filesystem side effect")

    monkeypatch.setattr(document_versions, "promote_version", move_and_report_failure)

    with pytest.raises(VersionPromotionError) as caught:
        promote_staged_version(
            db,
            versions_root,
            document_id=document_id,
            filename="restore.txt",
            staged_path=staged,
            content_hash=digest,
            uploaded_by=user_id,
        )

    final = versions_root / str(document_id) / "1" / "restore.txt"
    assert caught.value.recovery_required is False
    assert calls == 2
    assert staged.read_bytes() == body
    assert not final.exists()
    assert db.list_document_versions(document_id) == []


def test_failed_move_back_retains_the_final_file_version_and_permission_owner(
    tmp_path, material_db, monkeypatch
):
    import rag.document_versions as document_versions

    db, user_id = material_db
    versions_root = tmp_path / "versions"
    document_id = db.create_document_permission("retained.txt", "kb-1", user_id, 1, False)
    staged = versions_root / "staging" / "incoming-retained.txt"
    staged.parent.mkdir(parents=True)
    body = b"preserve recoverable final bytes"
    staged.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    real_move = document_versions.promote_version
    calls = 0

    def fail_compensation(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_move(source, destination)
            raise OSError("promotion raised after moving")
        raise OSError("move-back did not happen")

    monkeypatch.setattr(document_versions, "promote_version", fail_compensation)

    with pytest.raises(VersionPromotionError) as caught:
        promote_staged_version(
            db,
            versions_root,
            document_id=document_id,
            filename="retained.txt",
            staged_path=staged,
            content_hash=digest,
            uploaded_by=user_id,
        )

    final = versions_root / str(document_id) / "1" / "retained.txt"
    version = db.list_document_versions(document_id)[0]
    assert caught.value.recovery_required is True
    assert caught.value.version_id == version["id"]
    assert caught.value.file_path == final
    assert db.get_document_permission_by_id(document_id) is not None
    assert version["file_path"] == str(final)
    assert final.read_bytes() == body
    assert not staged.exists()


def test_ambiguous_stage_and_final_copies_keep_both_and_recovery_owner(
    tmp_path, material_db, monkeypatch
):
    import rag.document_versions as document_versions

    db, user_id = material_db
    versions_root = tmp_path / "versions"
    document_id = db.create_document_permission("copies.txt", "kb-1", user_id, 1, False)
    staged = versions_root / "staging" / "incoming-copies.txt"
    staged.parent.mkdir(parents=True)
    body = b"both copies need inspection"
    staged.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    calls = 0

    def leave_two_copies(source, destination):
        nonlocal calls
        calls += 1
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        raise OSError("copy happened but promotion did not complete cleanly")

    monkeypatch.setattr(document_versions, "promote_version", leave_two_copies)

    with pytest.raises(VersionPromotionError) as caught:
        promote_staged_version(
            db,
            versions_root,
            document_id=document_id,
            filename="copies.txt",
            staged_path=staged,
            content_hash=digest,
            uploaded_by=user_id,
        )

    final = versions_root / str(document_id) / "1" / "copies.txt"
    version = db.list_document_versions(document_id)[0]
    assert caught.value.recovery_required is True
    assert caught.value.version_id == version["id"]
    assert caught.value.file_path == final
    assert calls == 1
    assert staged.read_bytes() == body
    assert final.read_bytes() == body
    assert db.get_document_permission_by_id(document_id) is not None
    assert version["file_path"] == str(final)


def test_deferred_commit_failure_rolls_back_before_upload_compensation(
    isolated_api, make_user
):
    """A real deferred-FK COMMIT failure must not leave the shared writer poisoned."""
    user, headers = make_user("deferred-commit")
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    document_id, old_version_id, old_path, _old_job_ids = _seed_current_document(
        db, user["id"], versions_root, "deferred.txt", kb_id="rag_docs"
    )
    old_jobs = _job_rows(db, document_id)

    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "CREATE TABLE deferred_commit_parent (id INTEGER PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE deferred_commit_child ("
            "parent_id INTEGER NOT NULL, "
            "FOREIGN KEY(parent_id) REFERENCES deferred_commit_parent(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )
        conn.execute(
            f"CREATE TRIGGER fail_upload_job_at_commit "
            f"AFTER INSERT ON document_index_jobs "
            f"WHEN NEW.document_id={document_id} AND NEW.version_id!={old_version_id} "
            "BEGIN INSERT INTO deferred_commit_child(parent_id) VALUES (999); END"
        )

    response = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": ("deferred.txt", b"deferred transaction bytes", "text/plain")},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "material_promotion_failed"
    assert db._conn.in_transaction is False
    assert _job_rows(db, document_id) == old_jobs
    assert [version["id"] for version in db.list_document_versions(document_id)] == [
        old_version_id
    ]
    assert db.get_current_document_version(document_id)["id"] == old_version_id
    assert old_path.read_bytes() == b"old current material"
    assert not (versions_root / str(document_id) / "2" / "deferred.txt").exists()
    staged = list((versions_root / "staging").glob("*-deferred.txt"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"deferred transaction bytes"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM deferred_commit_child"
    ).fetchone()[0] == 0

    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE document_permissions SET metadata_version=metadata_version WHERE id=?",
            (document_id,),
        )
