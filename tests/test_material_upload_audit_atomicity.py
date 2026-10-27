"""Behavioral upload regressions for organization audit transaction boundaries."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from pathlib import Path

import pytest

from rag.materials.archives import ArchiveLimits, ArchiveService
from rag.materials.formats import build_default_format_registry
from rag.user_db import UserDB


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("team/notes.txt", b"synthetic archive member")
    return buffer.getvalue()


def _seed_org_kb(db, *, kb_id: str, admin_id: int) -> None:
    with db.locked_transaction(immediate=True) as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','Team','team')")
        conn.execute("INSERT INTO kb_metadata(kb_id,name,owner_id) VALUES(?,?,?)", (kb_id, "Synthetic KB", admin_id))
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES(?, 'TEAM', 'organization')",
            (kb_id,),
        )


def _current_version(db, document_id: int, path: Path, owner_id: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"previous published bytes")
    version_id = db.create_document_version(
        document_id, path.name, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), owner_id
    )
    db.activate_document_version(document_id, version_id, "kb_upload_audit", path.name)
    return version_id


@pytest.mark.parametrize("has_existing_document", [False, True], ids=["new-document", "existing-document"])
def test_document_upload_audit_failure_preserves_published_state_and_staged_bytes(
    isolated_api, make_user, has_existing_document: bool
) -> None:
    user, headers = make_user(f"upload-audit-{has_existing_document}", is_admin=True)
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    filename = f"audit-{has_existing_document}.txt"
    _seed_org_kb(db, kb_id="kb_upload_audit", admin_id=user["id"])

    document_id = None
    prior_version_id = None
    prior_jobs: list[tuple[int, int, str]] = []
    prior_owner = user["id"]
    if has_existing_document:
        prior_owner = db.create_user("upload-audit-original-owner", "Password-123!")
        document_id = db.create_document_permission(filename, "kb_upload_audit", prior_owner, 1, False)
        prior_path = versions_root / str(document_id) / "1" / filename
        prior_version_id = _current_version(db, document_id, prior_path, prior_owner)
        db.enqueue_material_job(document_id, prior_version_id)
        prior_jobs = [
            (row["id"], row["version_id"], row["status"])
            for row in db._conn.execute(
                "SELECT id,version_id,status FROM document_index_jobs WHERE document_id=? ORDER BY id",
                (document_id,),
            )
        ]

    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "CREATE TRIGGER reject_document_upload_audit "
            "BEFORE INSERT ON org_audit_events WHEN NEW.action='document_uploaded' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic audit outage'); END"
        )

    body = b"new synthetic document bytes"
    response = isolated_api.post(
        "/materials/upload?kb_id=kb_upload_audit",
        headers=headers,
        files={"file": (filename, body, "text/plain")},
    )

    assert response.status_code == 500
    if has_existing_document:
        assert document_id is not None and prior_version_id is not None
        permission = db.get_document_permission_by_id(document_id)
        assert permission["owner_id"] == prior_owner
        assert permission["current_version_id"] == prior_version_id
        versions = db.list_document_versions(document_id)
        assert [(row["id"], row["status"]) for row in versions] == [(prior_version_id, "current")]
        jobs = [
            (row["id"], row["version_id"], row["status"])
            for row in db._conn.execute(
                "SELECT id,version_id,status FROM document_index_jobs WHERE document_id=? ORDER BY id",
                (document_id,),
            )
        ]
        assert jobs == prior_jobs
        assert Path(versions[0]["file_path"]).read_bytes() == b"previous published bytes"
    else:
        assert db.get_document_permission(filename, "kb_upload_audit") is None

    staged_files = list((versions_root / "staging").rglob(f"*-{filename}"))
    assert len(staged_files) == 1
    assert staged_files[0].read_bytes() == body
    assert db._conn.in_transaction is False
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='document_uploaded'").fetchone()[0] == 0


def test_archive_upload_audit_failure_rolls_back_container_manifest_and_quarantine(isolated_api, make_user) -> None:
    user, headers = make_user("archive-upload-audit", is_admin=True)
    isolated_api._transport.raise_server_exceptions = False
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    _seed_org_kb(db, kb_id="kb_upload_audit", admin_id=user["id"])
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "CREATE TRIGGER reject_archive_upload_audit "
            "BEFORE INSERT ON org_audit_events WHEN NEW.action='archive_uploaded' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic audit outage'); END"
        )

    response = isolated_api.post(
        "/materials/upload?kb_id=kb_upload_audit",
        headers=headers,
        files={"file": ("bundle.zip", _zip_bytes(), "application/zip")},
    )

    assert response.status_code == 500
    assert db.get_document_permission("bundle.zip", "kb_upload_audit") is None
    assert db._conn.execute("SELECT COUNT(*) FROM material_archive_manifests").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM material_archive_entries").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='archive_uploaded'").fetchone()[0] == 0
    staging_root = versions_root / "staging"
    assert not staging_root.exists() or not any(path.is_file() for path in staging_root.rglob("*"))
    assert db._conn.in_transaction is False


@pytest.mark.parametrize(
    "failure",
    ["entry", "audit_insert", "deferred_commit"],
    ids=["entry-insert", "audit-insert", "deferred-commit"],
)
def test_archive_upload_sql_failure_rolls_back_new_rows_and_keeps_source_and_old_container(
    tmp_path: Path, failure: str
) -> None:
    db = UserDB(tmp_path / "archive-audit.db")
    source = tmp_path / "bundle.zip"
    source_bytes = _zip_bytes()
    source.write_bytes(source_bytes)
    try:
        uploader = db.create_user("archive-uploader", "Password-123!")
        owner = db.create_user("archive-owner", "Password-123!")
        db.set_user_admin(uploader, True)
        _seed_org_kb(db, kb_id="kb_upload_audit", admin_id=uploader)

        filename = "bundle.zip"
        document_id = db.create_document_permission(filename, "kb_upload_audit", owner, 1, False)
        versions_root = tmp_path / "versions"
        prior_path = versions_root / str(document_id) / "1" / filename
        prior_version_id = _current_version(db, document_id, prior_path, owner)
        service = ArchiveService(
            db,
            versions_root,
            build_default_format_registry(),
            ArchiveLimits(),
        )

        with db.locked_transaction(immediate=True) as conn:
            if failure == "entry":
                conn.execute(
                    "CREATE TRIGGER reject_archive_entry "
                    "BEFORE INSERT ON material_archive_entries "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic entry failure'); END"
                )
            elif failure == "audit_insert":
                conn.execute(
                    "CREATE TRIGGER reject_archive_upload_audit "
                    "BEFORE INSERT ON org_audit_events WHEN NEW.action='archive_uploaded' "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic audit outage'); END"
                )
            else:
                conn.execute("CREATE TABLE archive_commit_parent (id INTEGER PRIMARY KEY)")
                conn.execute(
                    "CREATE TABLE archive_commit_child (parent_id INTEGER NOT NULL, "
                    "FOREIGN KEY(parent_id) REFERENCES archive_commit_parent(id) "
                    "DEFERRABLE INITIALLY DEFERRED)"
                )
                conn.execute(
                    "CREATE TRIGGER fail_archive_upload_commit "
                    "AFTER INSERT ON material_archive_entries "
                    "BEGIN INSERT INTO archive_commit_child(parent_id) VALUES(999); END"
                )

        with pytest.raises(sqlite3.IntegrityError):
            service.upload_zip(filename, "kb_upload_audit", db.get_user_by_id(uploader), source)

        permission = db.get_document_permission_by_id(document_id)
        assert permission["owner_id"] == owner
        assert permission["current_version_id"] == prior_version_id
        versions = db.list_document_versions(document_id)
        assert [(row["id"], row["status"]) for row in versions] == [(prior_version_id, "current")]
        assert db._conn.execute("SELECT COUNT(*) FROM material_archive_manifests").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM material_archive_entries").fetchone()[0] == 0
        assert (
            db._conn.execute("SELECT COUNT(*) FROM org_audit_events WHERE action='archive_uploaded'").fetchone()[0] == 0
        )
        assert list((versions_root / "staging").rglob("*-bundle.zip")) == []
        assert source.read_bytes() == source_bytes
        assert service.last_container_id is None
        assert service.last_manifest_id is None
        assert db._conn.in_transaction is False
    finally:
        db.close()


def test_archive_upload_hashes_source_before_opening_write_transaction(tmp_path: Path, monkeypatch) -> None:
    import rag.materials.archives as archives

    db = UserDB(tmp_path / "archive-hash.db")
    source = tmp_path / "bundle.zip"
    source_bytes = _zip_bytes()
    source.write_bytes(source_bytes)
    try:
        actor_id = db.create_user("archive-hash-uploader", "Password-123!")
        original_hash = archives._sha256_file
        hash_calls: list[str] = []

        def observe_hash(path: Path) -> str:
            assert db._conn.in_transaction is False
            digest = original_hash(path)
            hash_calls.append(digest)
            return digest

        monkeypatch.setattr(archives, "_sha256_file", observe_hash)
        service = ArchiveService(db, tmp_path / "versions", build_default_format_registry(), ArchiveLimits())

        preview = service.upload_zip("bundle.zip", "rag_docs", db.get_user_by_id(actor_id), source)

        expected_hash = hashlib.sha256(source_bytes).hexdigest()
        assert hash_calls == [expected_hash]
        versions = db.list_document_versions(preview.container_document_id)
        assert len(versions) == 1
        assert versions[0]["content_hash"] == expected_hash
    finally:
        db.close()
