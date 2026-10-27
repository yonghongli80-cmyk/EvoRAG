import json
from pathlib import Path

import pytest

from rag.backups.models import BackupError
from rag.backups.service import create_backup, restore_test, verify_backup
from tests.backup_fixtures import create_backup_sources, make_backup_context


@pytest.fixture
def complete_backup(tmp_path):
    context = make_backup_context(tmp_path)
    create_backup_sources(context)
    return create_backup(context, "manual", now=1_777_000_000.0).path


@pytest.fixture
def restore_roots(tmp_path):
    return {
        "project": tmp_path / "project",
        "data": tmp_path / "project" / "data",
        "qdrant": tmp_path / "project" / "qdrant_data",
        "backup": tmp_path / "backups",
    }


def test_restore_test_copies_and_revalidates_backup(complete_backup, tmp_path):
    report = restore_test(complete_backup, tmp_path / "isolation", protected_roots=())

    assert report.status == "verified"
    assert report.vector_rebuild_required is True
    assert (report.target / "restore-report.json").is_file()
    assert verify_backup(report.target).status == "verified"


@pytest.mark.parametrize("target_kind", ["project", "data", "qdrant", "backup"])
def test_restore_rejects_protected_targets(complete_backup, restore_roots, target_kind):
    with pytest.raises(BackupError, match="restore_target_protected"):
        restore_test(
            complete_backup,
            restore_roots[target_kind],
            protected_roots=tuple(restore_roots.values()),
        )


def test_restore_rejects_nonempty_destination(complete_backup, tmp_path):
    destination = tmp_path / "isolation" / complete_backup.name
    destination.mkdir(parents=True)
    (destination / "existing.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(BackupError, match="restore_target_not_empty"):
        restore_test(complete_backup, tmp_path / "isolation", protected_roots=())

    assert (destination / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_restore_report_contains_no_absolute_paths(complete_backup, tmp_path):
    report = restore_test(complete_backup, tmp_path / "isolation", protected_roots=())
    payload = (report.target / "restore-report.json").read_text(encoding="utf-8")

    assert str(tmp_path) not in payload
    assert json.loads(payload)["vector_rebuild_required"] is True


def test_restore_report_write_failure_cleans_temporary_and_allows_retry(complete_backup, tmp_path, monkeypatch):
    isolation_root = tmp_path / "isolation"
    backup_id = complete_backup.name
    destination = isolation_root / backup_id
    original_write_bytes = Path.write_bytes
    report_write_attempts = 0

    def fail_first_report_write(path, payload):
        nonlocal report_write_attempts
        if path.name == "restore-report.json":
            report_write_attempts += 1
            if report_write_attempts == 1:
                raise OSError("simulated report write failure")
        return original_write_bytes(path, payload)

    monkeypatch.setattr(Path, "write_bytes", fail_first_report_write)

    with pytest.raises(OSError, match="simulated report write failure"):
        restore_test(complete_backup, isolation_root, protected_roots=())

    assert not destination.exists()
    assert not list(isolation_root.glob(f".{backup_id}.restore-*"))

    report = restore_test(complete_backup, isolation_root, protected_roots=())

    assert report.target == destination
    assert (destination / "restore-report.json").is_file()
