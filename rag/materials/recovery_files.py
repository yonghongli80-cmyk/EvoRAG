"""Confined, hash-checked file operations for staging recovery.

This module intentionally contains no database, configuration, or service
dependencies. Callers must still prevent concurrent external filesystem
writers while a recovery operation is in progress.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_CHUNK_SIZE = 64 * 1024


class RecoveryFileError(Exception):
    """A recovery file operation failed with a stable machine-readable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RecoveryFile:
    path: Path
    sha256: str
    byte_size: int


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError as exc:
        raise RecoveryFileError("path_check_failed") from exc


def _validate_root(root: Path) -> Path:
    root_path = Path(root)
    if not root_path.is_absolute() or ".." in root_path.parts:
        raise RecoveryFileError("invalid_root")
    root_path = Path(os.path.abspath(root_path))

    # Check every existing component, including ancestors above the root.
    current = Path(root_path.anchor)
    for part in root_path.parts[1:]:
        current = current / part
        if _is_link(current):
            raise RecoveryFileError("root_link")
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise RecoveryFileError("invalid_root") from exc
        except OSError as exc:
            raise RecoveryFileError("path_check_failed") from exc
        if current == root_path:
            if not stat.S_ISDIR(info.st_mode):
                raise RecoveryFileError("invalid_root")
        elif not stat.S_ISDIR(info.st_mode):
            raise RecoveryFileError("root_link")
    if len(root_path.parts) == 1:
        if _is_link(root_path):
            raise RecoveryFileError("root_link")
        try:
            if not stat.S_ISDIR(root_path.lstat().st_mode):
                raise RecoveryFileError("invalid_root")
        except OSError as exc:
            raise RecoveryFileError("path_check_failed") from exc

    try:
        return root_path.resolve(strict=True)
    except OSError as exc:
        raise RecoveryFileError("invalid_root") from exc


def _validate_sha256(expected_sha256: str) -> None:
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise RecoveryFileError("invalid_hash")


def _candidate_path(root_real: Path, path: str | Path) -> Path:
    try:
        candidate = Path(path)
    except (TypeError, ValueError) as exc:
        raise RecoveryFileError("invalid_path") from exc
    if ".." in candidate.parts:
        raise RecoveryFileError("path_escape")
    if not candidate.is_absolute():
        candidate = root_real / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root_real)
    except ValueError as exc:
        raise RecoveryFileError("path_escape") from exc

    current = root_real
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        if _is_link(current):
            raise RecoveryFileError("path_link")
        try:
            info = current.lstat()
        except FileNotFoundError:
            # Missing destination tails are valid. No deeper component can
            # exist beneath a missing parent without a concurrent race.
            break
        except OSError as exc:
            raise RecoveryFileError("path_check_failed") from exc
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise RecoveryFileError("not_directory")
        else:
            if not stat.S_ISREG(info.st_mode):
                raise RecoveryFileError("not_regular_file")
            if info.st_nlink != 1:
                raise RecoveryFileError("hardlink")
    return candidate


def confined_recovery_path(root: Path, path: str | Path) -> Path:
    """Return a lexical path beneath an ordinary, link-free root.

    Missing final paths (and missing destination parents) are permitted.
    Existing ancestors may not be links or non-directories; existing files
    must be regular files with a single hard link.
    """

    root_real = _validate_root(root)
    return _candidate_path(root_real, path)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
    )


def _open_source(root: Path, path: str | Path) -> tuple[Path, int, os.stat_result]:
    source_path = confined_recovery_path(root, path)
    try:
        before_path = source_path.lstat()
        if _is_link(source_path):
            raise RecoveryFileError("path_link")
        if not stat.S_ISREG(before_path.st_mode):
            raise RecoveryFileError("not_regular_file")
        if before_path.st_nlink != 1:
            raise RecoveryFileError("hardlink")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(source_path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            os.close(fd)
            raise RecoveryFileError("not_regular_file")
        if _identity(before_path) != _identity(opened):
            os.close(fd)
            raise RecoveryFileError("file_changed")
        return source_path, fd, opened
    except RecoveryFileError:
        raise
    except OSError as exc:
        raise RecoveryFileError("read_failed") from exc


def _check_source_unchanged(path: Path, fd: int, before: os.stat_result) -> None:
    try:
        after_handle = os.fstat(fd)
        after_path = path.lstat()
        if _is_link(path) or _identity(before) != _identity(after_handle):
            raise RecoveryFileError("file_changed")
        if _identity(before) != _identity(after_path):
            raise RecoveryFileError("file_changed")
    except RecoveryFileError:
        raise
    except OSError as exc:
        raise RecoveryFileError("file_changed") from exc


def _read_hash(path: Path, fd: int, before: os.stat_result) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    with os.fdopen(fd, "rb", closefd=False) as stream:
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            byte_size += len(chunk)
    _check_source_unchanged(path, fd, before)
    if byte_size != before.st_size:
        raise RecoveryFileError("file_changed")
    return digest.hexdigest(), byte_size


def verify_recovery_file(root: Path, path: str | Path, expected_sha256: str) -> RecoveryFile:
    """Hash a confined regular file and reject any detected read-time change."""

    _validate_sha256(expected_sha256)
    source_path, fd, before = _open_source(root, path)
    try:
        actual_hash, byte_size = _read_hash(source_path, fd, before)
    except RecoveryFileError:
        raise
    except OSError as exc:
        raise RecoveryFileError("read_failed") from exc
    finally:
        os.close(fd)
    if actual_hash != expected_sha256:
        raise RecoveryFileError("hash_mismatch")
    return RecoveryFile(source_path, actual_hash, byte_size)


def _create_destination_parents(root_real: Path, destination: Path) -> None:
    relative_parent = destination.parent.relative_to(root_real)
    current = root_real
    for part in relative_parent.parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise RecoveryFileError("copy_failed") from exc
        if _is_link(current):
            raise RecoveryFileError("path_link")
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise RecoveryFileError("not_directory")
        except OSError as exc:
            raise RecoveryFileError("copy_failed") from exc


def copy_recovery_file(
    root: Path,
    source: str | Path,
    destination: str | Path,
    expected_sha256: str,
) -> RecoveryFile:
    """Copy a verified file using exclusive creation, preserving both paths."""

    _validate_sha256(expected_sha256)
    root_real = _validate_root(root)
    source_path = _candidate_path(root_real, source)
    destination_path = _candidate_path(root_real, destination)

    # Validate source before considering an existing destination.
    verify_recovery_file(root_real, source_path, expected_sha256)
    try:
        destination_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RecoveryFileError("path_check_failed") from exc
    else:
        try:
            return verify_recovery_file(root_real, destination_path, expected_sha256)
        except RecoveryFileError as exc:
            if exc.code == "hash_mismatch":
                raise RecoveryFileError("destination_exists") from exc
            raise

    _create_destination_parents(root_real, destination_path)
    # Revalidate after parent creation, immediately before exclusive create.
    destination_path = _candidate_path(root_real, destination_path)
    source_path, source_fd, source_before = _open_source(root_real, source_path)
    destination_fd: int | None = None
    digest = hashlib.sha256()
    byte_size = 0
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            destination_fd = os.open(destination_path, flags, 0o600)
        except FileExistsError:
            # Another matching recovery may have completed concurrently.
            return verify_recovery_file(root_real, destination_path, expected_sha256)
        with os.fdopen(source_fd, "rb", closefd=False) as src, os.fdopen(destination_fd, "wb", closefd=False) as dst:
            while True:
                chunk = src.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                dst.write(chunk)
                byte_size += len(chunk)
            dst.flush()
            os.fsync(destination_fd)
        _check_source_unchanged(source_path, source_fd, source_before)
        actual_hash = digest.hexdigest()
        if byte_size != source_before.st_size:
            raise RecoveryFileError("file_changed")
        if actual_hash != expected_sha256:
            raise RecoveryFileError("hash_mismatch")
        copied = verify_recovery_file(root_real, destination_path, expected_sha256)
        if copied.byte_size != byte_size:
            raise RecoveryFileError("copy_failed")
        return copied
    except RecoveryFileError:
        raise
    except OSError as exc:
        raise RecoveryFileError("copy_failed") from exc
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)
