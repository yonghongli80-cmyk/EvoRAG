from __future__ import annotations

import hashlib
import os
import stat
import subprocess

import pytest

from rag.materials.recovery_files import (
    RecoveryFileError,
    confined_recovery_path,
    copy_recovery_file,
    verify_recovery_file,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_verify_rejects_wrong_hash(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    source = root / "document.bin"
    source.write_bytes(b"version bytes")

    with pytest.raises(RecoveryFileError) as error:
        verify_recovery_file(root, source, digest(b"other bytes"))

    assert error.value.code == "hash_mismatch"


def test_verify_accepts_ordinary_file_with_path_handle_stat_semantics(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    source = root / "document.bin"
    contents = b"ordinary verification smoke"
    source.write_bytes(contents)

    result = verify_recovery_file(root, source, digest(contents))

    assert result.path == source
    assert result.sha256 == digest(contents)
    assert result.byte_size == len(contents)


@pytest.mark.parametrize("relative", ["../escape.bin", "nested/../../escape.bin"])
def test_confined_path_rejects_parent_traversal(tmp_path, relative):
    root = tmp_path / "versions"
    root.mkdir()

    with pytest.raises(RecoveryFileError) as error:
        confined_recovery_path(root, relative)

    assert error.value.code == "path_escape"


def test_confined_path_rejects_symlinked_parent_and_file(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "source.bin"
    source.write_bytes(b"data")
    parent_link = root / "linked"
    try:
        parent_link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(RecoveryFileError) as error:
        confined_recovery_path(root, parent_link / "source.bin")
    assert error.value.code == "path_link"

    parent_link.unlink()
    file_link = root / "source.bin"
    try:
        file_link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(RecoveryFileError) as error:
        confined_recovery_path(root, file_link)
    assert error.value.code == "path_link"


def test_confined_path_rejects_symlink_in_root_ancestry(tmp_path):
    target = tmp_path / "real-parent"
    root = target / "versions"
    root.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(RecoveryFileError) as error:
        confined_recovery_path(alias / "versions", "missing.bin")
    assert error.value.code == "root_link"


def test_confined_path_rejects_junction_in_root_ancestry(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    target = tmp_path / "real-parent"
    root = target / "versions"
    root.mkdir(parents=True)
    alias = tmp_path / "junction-parent"
    command = "New-Item -ItemType Junction -Path '{}' -Target '{}' | Out-Null".format(
        str(alias).replace("'", "''"), str(target).replace("'", "''")
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr.strip()}")

    assert alias.is_junction()
    with pytest.raises(RecoveryFileError) as error:
        confined_recovery_path(alias / "versions", "missing.bin")
    assert error.value.code == "root_link"


def test_confined_path_rejects_hardlink(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"data")
    alias = root / "alias.bin"
    try:
        os.link(source, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hardlink creation is unavailable on this Windows host")

    with pytest.raises(RecoveryFileError) as error:
        confined_recovery_path(root, source)
    assert error.value.code == "hardlink"


def test_confined_path_rejects_special_file(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    fifo = root / "pipe"
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    os.mkfifo(fifo)

    with pytest.raises(RecoveryFileError) as error:
        confined_recovery_path(root, fifo)
    assert error.value.code == "not_regular_file"


def test_verify_rejects_file_that_changes_during_read(tmp_path, monkeypatch):
    from rag.materials import recovery_files

    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"stable bytes")
    original_fstat = recovery_files.os.fstat
    calls = 0

    def changed_fstat(fd):
        nonlocal calls
        result = original_fstat(fd)
        calls += 1
        if calls == 2:
            values = list(result)
            values[stat.ST_MTIME] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(recovery_files.os, "fstat", changed_fstat)
    with pytest.raises(RecoveryFileError) as error:
        verify_recovery_file(root, source, digest(b"stable bytes"))
    assert error.value.code == "file_changed"


def test_verify_rejects_ctime_change_during_read(tmp_path, monkeypatch):
    from rag.materials import recovery_files

    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"same size")
    original_fstat = recovery_files.os.fstat
    calls = 0

    class ChangedCtime:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.st_ctime_ns = wrapped.st_ctime_ns + 1

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def changed_ctime_fstat(fd):
        nonlocal calls
        result = original_fstat(fd)
        calls += 1
        if calls == 2:
            return ChangedCtime(result)
        return result

    monkeypatch.setattr(recovery_files.os, "fstat", changed_ctime_fstat)
    with pytest.raises(RecoveryFileError) as error:
        verify_recovery_file(root, source, digest(b"same size"))
    assert error.value.code == "file_changed"


def test_open_source_closes_descriptor_when_fstat_fails(tmp_path, monkeypatch):
    from rag.materials import recovery_files

    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"descriptor")
    original_open = recovery_files.os.open
    original_fstat = recovery_files.os.fstat
    opened_fds = []

    def tracked_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened_fds.append(fd)
        return fd

    def failed_fstat(_fd):
        raise OSError("simulated fstat failure")

    monkeypatch.setattr(recovery_files.os, "open", tracked_open)
    monkeypatch.setattr(recovery_files.os, "fstat", failed_fstat)
    with pytest.raises(RecoveryFileError) as error:
        verify_recovery_file(root, source, digest(b"descriptor"))

    assert error.value.code == "read_failed"
    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        original_fstat(opened_fds[0])


def test_copy_creates_preserved_copy_and_reuses_matching_retry(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    destination = root / "nested" / "copy.bin"
    contents = b"recovery payload" * 100
    source.write_bytes(contents)

    first = copy_recovery_file(root, source, destination, digest(contents))
    second = copy_recovery_file(root, source, destination, digest(contents))

    assert first == second
    assert destination.read_bytes() == contents
    assert source.read_bytes() == contents
    assert first.byte_size == len(contents)


def test_copy_never_overwrites_mismatched_destination_or_source(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    destination = root / "destination.bin"
    source.write_bytes(b"source")
    destination.write_bytes(b"keep destination")

    with pytest.raises(RecoveryFileError) as error:
        copy_recovery_file(root, source, destination, digest(b"source"))

    assert error.value.code == "destination_exists"
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"keep destination"


def test_copy_failure_preserves_source_and_partial_copy(tmp_path, monkeypatch):
    from rag.materials import recovery_files

    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    destination = root / "copy.bin"
    source.write_bytes(b"source stays")
    original_fsync = recovery_files.os.fsync

    def failed_fsync(fd):
        original_fsync(fd)
        raise OSError("simulated flush failure")

    monkeypatch.setattr(recovery_files.os, "fsync", failed_fsync)
    with pytest.raises(RecoveryFileError) as error:
        copy_recovery_file(root, source, destination, digest(b"source stays"))

    assert error.value.code == "copy_failed"
    assert source.read_bytes() == b"source stays"
    assert destination.exists()


def test_copy_rejects_destination_escape_without_touching_source(tmp_path):
    root = tmp_path / "versions"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"source")

    with pytest.raises(RecoveryFileError) as error:
        copy_recovery_file(root, source, "../outside.bin", digest(b"source"))

    assert error.value.code == "path_escape"
    assert source.read_bytes() == b"source"
