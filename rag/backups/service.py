from __future__ import annotations

import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import sqlite3

from rag.backups.models import (
    BackupError,
    BackupManifest,
    BackupVerification,
    DatabaseEntry,
    FileEntry,
)


_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}\n?\Z")


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


def write_manifest(staging: Path, manifest: BackupManifest) -> None:
    manifest_bytes = manifest.to_json_bytes()
    checksum = hashlib.sha256(manifest_bytes).hexdigest().encode("ascii") + b"\n"
    try:
        with (staging / "manifest.json").open("xb") as output:
            output.write(manifest_bytes)
        with (staging / "manifest.sha256").open("xb") as output:
            output.write(checksum)
    except OSError as error:
        raise BackupError("manifest_write_failed") from error


def _safe_posix_path(raw_path: str) -> PurePosixPath:
    if "\\" in raw_path:
        raise BackupError("unsafe_manifest_path")
    path = PurePosixPath(raw_path)
    if (
        not raw_path
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != raw_path
    ):
        raise BackupError("unsafe_manifest_path")
    return path


def _read_manifest(backup_dir: Path) -> BackupManifest:
    manifest_path = backup_dir / "manifest.json"
    checksum_path = backup_dir / "manifest.sha256"
    try:
        if manifest_path.is_symlink() or checksum_path.is_symlink():
            raise BackupError("symbolic_link_rejected")
        if manifest_path.stat().st_size > _MANIFEST_MAX_BYTES:
            raise BackupError("manifest_too_large")
        manifest_bytes = manifest_path.read_bytes()
        checksum_bytes = checksum_path.read_bytes()
    except BackupError:
        raise
    except OSError as error:
        raise BackupError("manifest_missing") from error

    try:
        checksum_text = checksum_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise BackupError("manifest_checksum_invalid") from error
    if not _CHECKSUM_PATTERN.fullmatch(checksum_text):
        raise BackupError("manifest_checksum_invalid")
    expected_checksum = checksum_text.rstrip("\n")
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_checksum:
        raise BackupError("manifest_checksum_mismatch")

    try:
        payload = json.loads(manifest_bytes)
        if not isinstance(payload, dict):
            raise TypeError
        return BackupManifest.from_dict(payload)
    except BackupError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise BackupError("invalid_manifest") from error


def _payload_files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    found: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError("symbolic_link_rejected")
        if path.is_file():
            found.add(path.relative_to(root.parent).as_posix())
    return found


def _verify_sqlite(path: Path) -> None:
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as database:
            result = database.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise BackupError("sqlite_integrity_failed") from error
    if not result or result[0] != "ok":
        raise BackupError("sqlite_integrity_failed")


def verify_backup(backup_dir: Path) -> BackupVerification:
    if backup_dir.is_symlink():
        raise BackupError("symbolic_link_rejected")
    try:
        resolved_backup = backup_dir.resolve(strict=True)
    except OSError as error:
        raise BackupError("backup_missing") from error
    if not resolved_backup.is_dir():
        raise BackupError("backup_missing")

    manifest = _read_manifest(resolved_backup)
    database_paths: set[str] = set()
    file_paths: set[str] = set()

    for entry in manifest.databases:
        path = _safe_posix_path(entry.path)
        if not path.parts or path.parts[0] != "sqlite" or entry.path in database_paths:
            raise BackupError("unsafe_manifest_path")
        database_paths.add(entry.path)

    for entry in manifest.files:
        path = _safe_posix_path(entry.path)
        payload_path = (PurePosixPath("files") / path).as_posix()
        if payload_path in file_paths:
            raise BackupError("unsafe_manifest_path")
        file_paths.add(payload_path)

    required_keys = tuple(_safe_posix_path(key).as_posix() for key in manifest.required_file_keys)
    if len(required_keys) != len(set(required_keys)):
        raise BackupError("unsafe_manifest_path")
    if not set(required_keys).issubset(manifest.file_keys):
        raise BackupError("required_file_missing")

    actual_payloads = _payload_files(resolved_backup / "sqlite") | _payload_files(
        resolved_backup / "files"
    )
    expected_payloads = database_paths | file_paths
    if actual_payloads != expected_payloads:
        raise BackupError("unexpected_backup_file")

    for entry in manifest.databases:
        path = resolved_backup.joinpath(*PurePosixPath(entry.path).parts)
        if path.is_symlink():
            raise BackupError("symbolic_link_rejected")
        _verify_sqlite(path)
        try:
            size = path.stat().st_size
            digest = sha256_file(path)
        except OSError as error:
            raise BackupError("database_missing") from error
        if size != entry.size or digest != entry.sha256:
            raise BackupError("database_hash_mismatch")

    for entry in manifest.files:
        path = resolved_backup.joinpath("files", *PurePosixPath(entry.path).parts)
        if path.is_symlink():
            raise BackupError("symbolic_link_rejected")
        try:
            size = path.stat().st_size
            digest = sha256_file(path)
        except OSError as error:
            raise BackupError("file_missing") from error
        if size != entry.size or digest != entry.sha256:
            raise BackupError("file_hash_mismatch")

    total_bytes = sum(entry.size for entry in manifest.databases) + sum(
        entry.size for entry in manifest.files
    )
    if manifest.total_files != len(expected_payloads) or manifest.total_bytes != total_bytes:
        raise BackupError("manifest_totals_mismatch")

    return BackupVerification(
        backup_id=manifest.backup_id,
        status="verified",
        file_count=len(manifest.files),
        database_count=len(manifest.databases),
        total_bytes=total_bytes,
        vector_rebuild_required=manifest.vector_rebuild_required,
    )
