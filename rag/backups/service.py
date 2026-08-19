from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sqlite3

from rag.backups.models import BackupError, DatabaseEntry, FileEntry


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_sqlite(source: Path, target: Path) -> DatabaseEntry:
    if not source.is_file():
        raise BackupError("required_database_missing")

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb"):
            pass
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
            with sqlite3.connect(target) as target_db:
                source_db.backup(target_db)
                target_db.execute("PRAGMA journal_mode=DELETE")
    except (OSError, sqlite3.Error) as error:
        raise BackupError("sqlite_snapshot_failed") from error

    try:
        target_uri = f"file:{target.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(target_uri, uri=True, timeout=30) as snapshot_db:
            result = snapshot_db.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise BackupError("sqlite_integrity_failed") from error

    integrity = str(result[0]) if result else ""
    if integrity != "ok":
        raise BackupError("sqlite_integrity_failed")

    return DatabaseEntry(
        name=source.stem,
        path=target.name,
        size=target.stat().st_size,
        sha256=sha256_file(target),
        integrity=integrity,
        required=True,
    )


def copy_file_verified(source: Path, target: Path, logical_path: str) -> FileEntry:
    if source.is_symlink():
        raise BackupError("symbolic_link_rejected")

    try:
        before_stat = source.stat()
        before_hash = sha256_file(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        after_stat = source.stat()
        after_hash = sha256_file(source)
        target_hash = sha256_file(target)
    except OSError as error:
        raise BackupError("file_copy_failed") from error

    before_metadata = (before_stat.st_size, before_stat.st_mtime_ns)
    after_metadata = (after_stat.st_size, after_stat.st_mtime_ns)
    if (
        before_metadata != after_metadata
        or before_hash != after_hash
        or target_hash != before_hash
    ):
        raise BackupError("source_changed_during_copy")

    return FileEntry(path=logical_path, size=before_stat.st_size, sha256=before_hash)
