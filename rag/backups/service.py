from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import sqlite3
import sys
import time
from typing import Iterator
import uuid

from rag.backups.models import (
    BackupContext,
    BackupError,
    BackupManifest,
    BackupRecord,
    BackupVerification,
    DatabaseEntry,
    FileEntry,
    RestoreReport,
    deterministic_json_bytes,
)


_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}\n?\Z")
_FORMAL_BACKUP_PATTERN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{12}\Z")
_TRANSIENT_DIRECTORIES = frozenset({"staging", "quarantine", "archive_work"})
_EXCLUDED_PAYLOADS = (
    "qdrant",
    "bm25",
    "embedding_cache",
    "index_state",
    "staging",
    "quarantine",
    "archive_work",
)
logger = logging.getLogger(__name__)


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
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=30)
        ) as source_db, closing(sqlite3.connect(target)) as target_db:
            source_db.backup(target_db)
            target_db.execute("PRAGMA journal_mode=DELETE")
    except (OSError, sqlite3.Error) as error:
        raise BackupError("sqlite_snapshot_failed") from error

    try:
        target_uri = f"file:{target.resolve().as_posix()}?mode=ro"
        with closing(
            sqlite3.connect(target_uri, uri=True, timeout=30)
        ) as snapshot_db:
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
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as database:
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


@contextmanager
def _exclusive_backup_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".backup.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.path.getsize(lock_path) == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise BackupError("backup_lock_failed") from error

    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise BackupError("backup_in_progress") from error
        yield
    finally:
        if locked:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _iter_source_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    if root.is_symlink():
        raise BackupError("symbolic_link_rejected")
    resolved_root = root.resolve()
    for current_root, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(
            name for name in directory_names if name not in _TRANSIENT_DIRECTORIES
        )
        current = Path(current_root)
        for directory_name in directory_names:
            if (current / directory_name).is_symlink():
                raise BackupError("symbolic_link_rejected")
        for file_name in sorted(file_names):
            source = current / file_name
            if source.is_symlink():
                raise BackupError("symbolic_link_rejected")
            try:
                source.resolve().relative_to(resolved_root)
            except (OSError, ValueError) as error:
                raise BackupError("source_path_outside_root") from error
            yield source


def _table_exists(database: sqlite3.Connection, table: str) -> bool:
    row = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _logical_key_for_reference(
    raw_path: str, context: BackupContext, preferred_root: str
) -> str:
    source_path = Path(raw_path)
    candidates: list[tuple[str, Path]] = []
    if source_path.is_absolute():
        candidates = list(context.file_roots.items())
    else:
        preferred = context.file_roots[preferred_root]
        candidates = [(preferred_root, preferred)]

    matches: list[str] = []
    for logical_root, root in candidates:
        try:
            relative = (
                source_path.resolve().relative_to(root.resolve())
                if source_path.is_absolute()
                else (root / source_path).resolve().relative_to(root.resolve())
            )
        except (OSError, ValueError):
            continue
        matches.append((PurePosixPath(logical_root) / PurePosixPath(relative.as_posix())).as_posix())
    if len(matches) != 1:
        raise BackupError("referenced_file_outside_roots")
    return matches[0]


def _required_file_keys(users_snapshot: Path, context: BackupContext) -> tuple[str, ...]:
    queries = (
        (
            "document_versions",
            "SELECT file_path FROM document_versions "
            "WHERE status IN ('current', 'historical')",
            "document_versions",
        ),
        (
            "kb_documents",
            "SELECT file_path FROM kb_documents "
            "WHERE status = 'indexed' AND file_path IS NOT NULL",
            "kb_files",
        ),
        (
            "structured_datasets",
            "SELECT storage_key FROM structured_datasets "
            "WHERE status IN ('ready', 'historical')",
            "structured_datasets",
        ),
    )
    keys: set[str] = set()
    uri = f"file:{users_snapshot.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as database:
            for table, query, preferred_root in queries:
                if not _table_exists(database, table):
                    continue
                for row in database.execute(query):
                    if row[0]:
                        keys.add(
                            _logical_key_for_reference(str(row[0]), context, preferred_root)
                        )
    except BackupError:
        raise
    except sqlite3.Error as error:
        raise BackupError("reference_validation_failed") from error
    return tuple(sorted(keys))


def _application_version() -> str:
    try:
        return version("rag-knowledge-base")
    except PackageNotFoundError:
        return "3.0.0"


def _remove_staging(staging: Path, backup_root: Path) -> None:
    try:
        resolved_root = backup_root.resolve()
        resolved_staging = staging.resolve()
        if resolved_staging.parent != resolved_root or not staging.name.startswith(
            ".incomplete-"
        ):
            raise BackupError("unsafe_staging_path")
        if staging.exists():
            shutil.rmtree(staging)
    except BackupError:
        raise
    except OSError as error:
        raise BackupError("staging_cleanup_failed") from error


def create_backup(
    context: BackupContext, trigger: str, now: float | None = None
) -> BackupRecord:
    validated = context.validate()
    timestamp = time.time() if now is None else now
    if trigger not in {"manual", "scheduled", "deploy"}:
        raise BackupError("invalid_backup_trigger")

    validated.backup_root.mkdir(parents=True, exist_ok=True)
    backup_id = datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    ) + f"-{uuid.uuid4().hex[:12]}"
    staging = validated.backup_root / f".incomplete-{backup_id}"
    final_path = validated.backup_root / backup_id

    with _exclusive_backup_lock(validated.backup_root):
        try:
            staging.mkdir()
            database_entries: list[DatabaseEntry] = []
            for name, source in sorted(validated.databases.items()):
                if not source.is_file():
                    if name in validated.required_databases:
                        raise BackupError("required_database_missing")
                    continue
                target = staging / "sqlite" / f"{name}.db"
                snapshot = snapshot_sqlite(source, target)
                database_entries.append(
                    replace(
                        snapshot,
                        name=name,
                        path=f"sqlite/{name}.db",
                        required=name in validated.required_databases,
                    )
                )

            file_entries: list[FileEntry] = []
            for logical_root, source_root in sorted(validated.file_roots.items()):
                for source in _iter_source_files(source_root):
                    relative = source.relative_to(source_root).as_posix()
                    logical_path = (
                        PurePosixPath(logical_root) / PurePosixPath(relative)
                    ).as_posix()
                    target = staging / "files" / logical_root / Path(relative)
                    file_entries.append(
                        copy_file_verified(source, target, logical_path)
                    )

            users_snapshot = staging / "sqlite" / "users.db"
            required_file_keys = _required_file_keys(users_snapshot, validated)
            copied_keys = frozenset(entry.path for entry in file_entries)
            if not set(required_file_keys).issubset(copied_keys):
                raise BackupError("referenced_file_missing")

            database_entries.sort(key=lambda entry: entry.name)
            file_entries.sort(key=lambda entry: entry.path)
            total_bytes = sum(entry.size for entry in database_entries) + sum(
                entry.size for entry in file_entries
            )
            manifest = BackupManifest(
                schema_version=1,
                backup_id=backup_id,
                created_at=timestamp,
                app_version=_application_version(),
                python_version=sys.version.split()[0],
                trigger=trigger,
                databases=tuple(database_entries),
                files=tuple(file_entries),
                required_file_keys=required_file_keys,
                excluded=_EXCLUDED_PAYLOADS,
                total_files=len(database_entries) + len(file_entries),
                total_bytes=total_bytes,
                qdrant_included=False,
                vector_rebuild_required=True,
            )
            write_manifest(staging, manifest)
            verification = verify_backup(staging)
            staging.replace(final_path)
            try:
                prune_verified_backups(
                    validated.backup_root, validated.retention_count
                )
            except BackupError:
                logger.warning("backup_retention_failed")
            return BackupRecord(
                backup_id=backup_id,
                created_at=timestamp,
                status=verification.status,
                path=final_path,
                file_count=verification.file_count,
                database_count=verification.database_count,
                total_bytes=verification.total_bytes,
                vector_rebuild_required=verification.vector_rebuild_required,
            )
        except Exception:
            _remove_staging(staging, validated.backup_root)
            raise


def list_verified_backups(root: Path) -> tuple[BackupRecord, ...]:
    if not root.exists():
        return ()
    records: list[BackupRecord] = []
    for path in sorted(root.iterdir()):
        if (
            not path.is_dir()
            or path.is_symlink()
            or not _FORMAL_BACKUP_PATTERN.fullmatch(path.name)
        ):
            continue
        try:
            verification = verify_backup(path)
            manifest = _read_manifest(path.resolve())
        except BackupError:
            continue
        records.append(
            BackupRecord(
                backup_id=manifest.backup_id,
                created_at=manifest.created_at,
                status=verification.status,
                path=path,
                file_count=verification.file_count,
                database_count=verification.database_count,
                total_bytes=verification.total_bytes,
                vector_rebuild_required=verification.vector_rebuild_required,
            )
        )
    return tuple(
        sorted(records, key=lambda record: (record.created_at, record.backup_id))
    )


def prune_verified_backups(root: Path, keep: int) -> tuple[str, ...]:
    if keep < 1:
        raise BackupError("invalid_retention_count")
    if not root.exists():
        return ()

    resolved_root = root.resolve()
    candidates = list_verified_backups(resolved_root)[:-keep]
    deleted: list[str] = []
    for record in candidates:
        candidate = record.path.resolve(strict=True)
        if (
            candidate.parent != resolved_root
            or candidate.name != record.backup_id
            or not _FORMAL_BACKUP_PATTERN.fullmatch(candidate.name)
        ):
            raise BackupError("unsafe_retention_path")
        verify_backup(candidate)
        try:
            shutil.rmtree(candidate)
        except OSError as error:
            raise BackupError("backup_prune_failed") from error
        deleted.append(record.backup_id)
    return tuple(deleted)


def create_if_due(
    context: BackupContext, trigger: str, now: float | None = None
) -> BackupRecord | None:
    timestamp = time.time() if now is None else now
    records = list_verified_backups(context.backup_root)
    if records:
        latest = records[-1]
        elapsed = timestamp - latest.created_at
        if elapsed < 0:
            logger.warning("backup_clock_moved_backwards")
            return None
        if elapsed < context.interval_days * 86400:
            return None
    return create_backup(context, trigger, now=timestamp)


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    return (
        first_resolved == second_resolved
        or first_resolved.is_relative_to(second_resolved)
        or second_resolved.is_relative_to(first_resolved)
    )


def restore_test(
    backup_dir: Path,
    isolation_root: Path,
    protected_roots: tuple[Path, ...],
) -> RestoreReport:
    source_verification = verify_backup(backup_dir)
    source = backup_dir.resolve(strict=True)
    isolation = isolation_root.resolve()

    if any(_paths_overlap(isolation, protected) for protected in protected_roots):
        raise BackupError("restore_target_protected")
    if _paths_overlap(source, isolation):
        raise BackupError("restore_source_overlap")

    destination = isolation / source_verification.backup_id
    if destination.exists():
        raise BackupError("restore_target_not_empty")

    isolation.mkdir(parents=True, exist_ok=True)
    temporary = isolation / (
        f".{source_verification.backup_id}.restore-{uuid.uuid4().hex[:12]}"
    )
    try:
        for path in source.rglob("*"):
            if path.is_symlink():
                raise BackupError("symbolic_link_rejected")
        shutil.copytree(source, temporary)
        verify_backup(temporary)
        temporary.rename(destination)
        restored_verification = verify_backup(destination)
        report_payload = {
            "backup_id": restored_verification.backup_id,
            "database_count": restored_verification.database_count,
            "file_count": restored_verification.file_count,
            "status": restored_verification.status,
            "total_bytes": restored_verification.total_bytes,
            "vector_rebuild_required": restored_verification.vector_rebuild_required,
        }
        (destination / "restore-report.json").write_bytes(
            deterministic_json_bytes(report_payload)
        )
        return RestoreReport(
            backup_id=restored_verification.backup_id,
            status=restored_verification.status,
            target=destination,
            file_count=restored_verification.file_count,
            database_count=restored_verification.database_count,
            vector_rebuild_required=restored_verification.vector_rebuild_required,
        )
    except Exception:
        if temporary.exists():
            resolved_temporary = temporary.resolve()
            if (
                resolved_temporary.parent != isolation
                or ".restore-" not in temporary.name
            ):
                raise BackupError("unsafe_restore_path")
            shutil.rmtree(temporary)
        raise
