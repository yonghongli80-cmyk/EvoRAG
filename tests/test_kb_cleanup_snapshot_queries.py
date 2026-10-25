from pathlib import Path

import pytest

from rag.kb_document_cleanup import resolve_kb_document_paths
from rag.kb_lifecycle import KBDeleteIncomplete
from rag.kb_lifecycle_cleanup import collect_kb_cleanup
from rag.user_db import UserDB


def _version(db, root: Path, document_id: int, owner_id: int, ordinal: int) -> Path:
    path = root / str(document_id) / str(ordinal) / f"version-{ordinal}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"version {ordinal}", encoding="utf-8")
    db.create_document_version(document_id, path.name, str(path), f"hash-{ordinal}", owner_id)
    return path


def test_collect_loads_each_permission_version_snapshot_once_and_refreshes_next_call(tmp_path, monkeypatch):
    db = UserDB(tmp_path / "users.db")
    try:
        owner_id = db.create_user("owner", "Password-123!")
        kb_id = "kb_snapshot"
        db.create_kb_metadata(kb_id, "Snapshot", owner_id)
        with_directory_id = db.create_document_permission("with-row.txt", kb_id, owner_id)
        without_directory_id = db.create_document_permission("without-row.txt", kb_id, owner_id)
        empty_id = db.create_document_permission("empty.txt", kb_id, owner_id)
        db.create_kb_document_pending(kb_id, "with-row.txt")
        db.create_kb_document_pending(kb_id, "empty.txt")
        first = _version(db, tmp_path / "versions", with_directory_id, owner_id, 1)
        _version(db, tmp_path / "versions", with_directory_id, owner_id, 2)
        _version(db, tmp_path / "versions", without_directory_id, owner_id, 1)

        original_list_documents = db.list_kb_documents
        original_list_versions = db.list_document_versions
        listed_rows = {row["filename"]: row for row in original_list_documents(kb_id)}
        duplicate_directory_rows = [
            listed_rows["with-row.txt"],
            listed_rows["with-row.txt"],
            listed_rows["empty.txt"],
            listed_rows["empty.txt"],
        ]
        monkeypatch.setattr(db, "list_kb_documents", lambda _kb_id: duplicate_directory_rows)
        counts = {}

        def counted_versions(document_id):
            counts[document_id] = counts.get(document_id, 0) + 1
            return original_list_versions(document_id)

        monkeypatch.setattr(db, "list_document_versions", counted_versions)
        versions_root = tmp_path / "versions"
        kb_files_root = tmp_path / "kb-files"
        datasets_root = tmp_path / "datasets"

        plan = collect_kb_cleanup(db, kb_id, versions_root, kb_files_root, datasets_root)

        assert counts == {
            with_directory_id: 1,
            without_directory_id: 1,
            empty_id: 1,
        }
        assert first in plan["paths"]
        assert len(plan["paths"]) == 3

        counts.clear()
        second = _version(db, versions_root, with_directory_id, owner_id, 3)
        refreshed = collect_kb_cleanup(db, kb_id, versions_root, kb_files_root, datasets_root)

        assert counts == {
            with_directory_id: 1,
            without_directory_id: 1,
            empty_id: 1,
        }
        assert second in refreshed["paths"]
        assert len(refreshed["paths"]) == 4
    finally:
        db.close()


def test_document_path_helper_keeps_default_version_loader(tmp_path):
    db = UserDB(tmp_path / "users.db")
    try:
        owner_id = db.create_user("owner", "Password-123!")
        kb_id = "kb_default_loader"
        db.create_kb_metadata(kb_id, "Default loader", owner_id)
        document_id = db.create_document_permission("notes.txt", kb_id, owner_id)
        path = _version(db, tmp_path / "versions", document_id, owner_id, 1)
        record = {"id": 1, "filename": "notes.txt", "file_path": None}

        assert resolve_kb_document_paths(db, kb_id, record, tmp_path / "versions", tmp_path / "kb-files") == [path]
    finally:
        db.close()


def test_archive_source_error_precedes_bad_version_path(tmp_path):
    db = UserDB(tmp_path / "users.db")
    try:
        owner_id = db.create_user("owner", "Password-123!")
        kb_id = "kb_archive_precedence"
        db.create_kb_metadata(kb_id, "Archive precedence", owner_id)
        document_id = db.create_document_permission("archive.zip", kb_id, owner_id)
        db.create_kb_document_pending(kb_id, "archive.zip")
        versions_root = tmp_path / "versions"
        bad_version = versions_root / "outside-owner" / "archive.zip"
        bad_version.parent.mkdir(parents=True)
        bad_version.write_bytes(b"version")
        version_id = db.create_document_version(document_id, "archive.zip", str(bad_version), "hash", owner_id)
        bad_source = versions_root / "outside-owner" / "source.zip"
        bad_source.write_bytes(b"archive")
        db.create_material_archive_manifest(document_id, version_id, str(bad_source), owner_id, 4_102_444_800)

        with pytest.raises(KBDeleteIncomplete) as error:
            collect_kb_cleanup(
                db,
                kb_id,
                versions_root,
                tmp_path / "kb-files",
                tmp_path / "datasets",
            )
        assert error.value.code == "archive_source_unowned"
    finally:
        db.close()
