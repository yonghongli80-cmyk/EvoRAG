import sqlite3
from pathlib import Path
import shutil

import pytest

from rag.backups.models import BackupError
from rag.backups.service import copy_file_verified, snapshot_sqlite


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
