import hashlib
import json
import sqlite3
from pathlib import Path
import shutil

import pytest

from rag.backups.models import BackupError
from rag.backups.service import copy_file_verified, snapshot_sqlite, verify_backup


def _json_bytes(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_literal_manifest(backup_dir, payload):
    manifest_bytes = _json_bytes(payload)
    (backup_dir / "manifest.json").write_bytes(manifest_bytes)
    checksum = hashlib.sha256(manifest_bytes).hexdigest()
    (backup_dir / "manifest.sha256").write_bytes(f"{checksum}\n".encode("ascii"))


@pytest.fixture
def complete_backup(tmp_path):
    backup_dir = tmp_path / "backup-1"
    database = backup_dir / "sqlite" / "users.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO users(name) VALUES ('Ada')")

    data_file = backup_dir / "files" / "upload" / "guide.txt"
    data_file.parent.mkdir(parents=True)
    data_file.write_text("guide", encoding="utf-8")

    payload = {
        "schema_version": 1,
        "backup_id": "backup-1",
        "created_at": 1_777_000_000.0,
        "app_version": "test",
        "python_version": "3.12",
        "trigger": "manual",
        "databases": [
            {
                "name": "users",
                "path": "sqlite/users.db",
                "size": database.stat().st_size,
                "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                "integrity": "ok",
                "required": True,
            }
        ],
        "files": [
            {
                "path": "upload/guide.txt",
                "size": data_file.stat().st_size,
                "sha256": hashlib.sha256(data_file.read_bytes()).hexdigest(),
            }
        ],
        "required_file_keys": ["upload/guide.txt"],
        "excluded": ["qdrant", "bm25", "embedding_cache"],
        "total_files": 2,
        "total_bytes": database.stat().st_size + data_file.stat().st_size,
        "qdrant_included": False,
        "vector_rebuild_required": True,
    }
    _write_literal_manifest(backup_dir, payload)
    return backup_dir


def rewrite_manifest_file_path(backup_dir, logical_path):
    payload = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    payload["files"][0]["path"] = logical_path
    _write_literal_manifest(backup_dir, payload)


def test_snapshot_sqlite_uses_a_consistent_database_copy(tmp_path):
    source = tmp_path / "source.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE notes(value TEXT NOT NULL)")
    writer.execute("INSERT INTO notes VALUES ('before')")
    writer.commit()

    entry = snapshot_sqlite(source, tmp_path / "snapshot.db")
    writer.execute("INSERT INTO notes VALUES ('after')")
    writer.commit()
    writer.close()

    restored = sqlite3.connect(tmp_path / "snapshot.db")
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT value FROM notes").fetchall() == [("before",)]
    restored.close()
    assert entry.integrity == "ok"
    assert entry.path == "snapshot.db"
    assert entry.size > 0
    assert len(entry.sha256) == 64


def test_snapshot_sqlite_does_not_copy_wal_or_shm_files(tmp_path):
    source = tmp_path / "source.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE notes(value TEXT NOT NULL)")
    writer.execute("INSERT INTO notes VALUES ('before')")
    writer.commit()

    target = tmp_path / "snapshot.db"
    snapshot_sqlite(source, target)
    writer.close()

    assert target.exists()
    assert not target.with_name(f"{target.name}-wal").exists()
    assert not target.with_name(f"{target.name}-shm").exists()


def test_copy_returns_only_logical_path_size_and_hash(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("内容", encoding="utf-8")
    target = tmp_path / "nested" / "target.txt"

    entry = copy_file_verified(source, target, "upload/source.txt")

    assert target.read_text(encoding="utf-8") == "内容"
    assert entry.path == "upload/source.txt"
    assert entry.size == len("内容".encode("utf-8"))
    assert len(entry.sha256) == 64
    assert str(tmp_path) not in repr(entry)


def test_copy_rejects_source_that_changes_during_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("before", encoding="utf-8")
    real_copy = shutil.copyfile

    def copy_then_mutate(src, dst):
        result = real_copy(src, dst)
        Path(src).write_text("after", encoding="utf-8")
        return result

    monkeypatch.setattr("rag.backups.service.shutil.copyfile", copy_then_mutate)
    with pytest.raises(BackupError, match="source_changed_during_copy"):
        copy_file_verified(source, tmp_path / "target.txt", "upload/source.txt")


def test_copy_rejects_symbolic_links(tmp_path):
    source = tmp_path / "real.txt"
    source.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symbolic links are unavailable for this Windows account")

    with pytest.raises(BackupError, match="symbolic_link_rejected"):
        copy_file_verified(link, tmp_path / "target.txt", "upload/link.txt")


def test_verify_accepts_complete_backup(complete_backup):
    result = verify_backup(complete_backup)

    assert result.status == "verified"
    assert result.file_count == 1
    assert result.database_count == 1
    assert result.vector_rebuild_required is True


def test_verify_rejects_changed_file(complete_backup):
    (complete_backup / "files/upload/guide.txt").write_text("tampered", encoding="utf-8")

    with pytest.raises(BackupError, match="file_hash_mismatch"):
        verify_backup(complete_backup)


def test_verify_rejects_corrupt_sqlite(complete_backup):
    database = complete_backup / "sqlite/users.db"
    database.write_bytes(b"not sqlite".ljust(database.stat().st_size, b"!"))

    with pytest.raises(BackupError, match="sqlite_integrity_failed"):
        verify_backup(complete_backup)


def test_verify_rejects_unlisted_database_payload(complete_backup):
    (complete_backup / "sqlite/rogue.db").write_bytes(b"unexpected")

    with pytest.raises(BackupError, match="unexpected_backup_file"):
        verify_backup(complete_backup)


@pytest.mark.parametrize(
    "logical_path", ["../escape.txt", "/absolute.txt", "upload/../escape.txt", r"upload\bad.txt"]
)
def test_verify_rejects_unsafe_manifest_paths(complete_backup, logical_path):
    rewrite_manifest_file_path(complete_backup, logical_path)

    with pytest.raises(BackupError, match="unsafe_manifest_path"):
        verify_backup(complete_backup)
