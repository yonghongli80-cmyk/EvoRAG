import json
import logging
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from config import settings
from rag.backups.models import BackupError
from rag.backups.scheduler import (
    BackupScheduler,
    backup_scheduler_is_alive,
    start_backup_scheduler,
    stop_backup_scheduler,
)
from rag.backups.service import (
    _exclusive_backup_lock,
    create_backup,
    create_if_due,
    list_verified_backups,
    snapshot_sqlite,
)
from scripts.backup import main as backup_cli_main
from tests.backup_fixtures import (
    create_backup_sources,
    make_backup_context,
    seed_verified_backups,
)

WEEK = 7 * 24 * 60 * 60


def wait_until(predicate):
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("scheduler condition timed out")


@pytest.fixture
def backup_context(tmp_path):
    context = make_backup_context(tmp_path)
    create_backup_sources(context)
    return context


def test_first_run_creates_backup(backup_context):
    assert create_if_due(
        backup_context, "scheduled", now=1_700_000_000.0
    ) is not None


def test_recent_backup_is_reused_without_duplicate(backup_context):
    first = create_if_due(backup_context, "scheduled", now=1_700_000_000.0)
    second = create_if_due(
        backup_context, "scheduled", now=1_700_000_000.0 + WEEK - 1
    )

    assert first is not None
    assert second is None
    assert len(list_verified_backups(backup_context.backup_root)) == 1


def test_exactly_seven_days_is_due(backup_context):
    create_if_due(backup_context, "scheduled", now=1_700_000_000.0)

    assert create_if_due(
        backup_context, "scheduled", now=1_700_000_000.0 + WEEK
    ) is not None


def test_future_manifest_time_does_not_create_duplicate(backup_context, caplog):
    create_if_due(backup_context, "scheduled", now=1_700_000_100.0)

    with caplog.at_level(logging.WARNING):
        result = create_if_due(
            backup_context, "scheduled", now=1_700_000_000.0
        )

    assert result is None
    assert "backup_clock_moved_backwards" in caplog.text


def test_ninth_success_keeps_latest_eight(backup_context):
    for week in range(9):
        create_if_due(
            backup_context, "scheduled", now=1_700_000_000.0 + week * WEEK
        )

    records = list_verified_backups(backup_context.backup_root)
    assert len(records) == 8
    assert records[0].created_at == 1_700_000_000.0 + WEEK


def test_failed_ninth_backup_does_not_prune_existing_eight(
    backup_context, monkeypatch
):
    seed_verified_backups(backup_context, count=8)

    def fail_snapshot(*_args, **_kwargs):
        raise BackupError("sqlite_snapshot_failed")

    monkeypatch.setattr("rag.backups.service.snapshot_sqlite", fail_snapshot)
    with pytest.raises(BackupError, match="sqlite_snapshot_failed"):
        create_backup(backup_context, "scheduled", now=1_800_000_000.0)

    assert len(list_verified_backups(backup_context.backup_root)) == 8


def test_concurrent_creation_is_rejected_by_lock(backup_context):
    entered = threading.Event()
    release = threading.Event()
    worker_errors = []
    real_snapshot = snapshot_sqlite

    def blocking_snapshot(source, target):
        entered.set()
        assert release.wait(timeout=2.0)
        return real_snapshot(source, target)

    def run_worker():
        try:
            create_backup(backup_context, "manual", 1_800_000_000.0)
        except Exception as error:
            worker_errors.append(error)

    with patch("rag.backups.service.snapshot_sqlite", blocking_snapshot):
        worker = threading.Thread(target=run_worker)
        worker.start()
        assert entered.wait(timeout=1.0)
        with pytest.raises(BackupError, match="backup_in_progress"):
            create_backup(backup_context, "manual", now=1_800_000_001.0)
        release.set()
        worker.join(timeout=3.0)

    assert worker.is_alive() is False
    assert worker_errors == []


def test_concurrent_due_decisions_recheck_after_the_first_backup(backup_context):
    """A due decision made before another creator finishes must not publish twice."""
    first_snapshot_started = threading.Event()
    release_first_snapshot = threading.Event()
    second_waiting_for_first = threading.Event()
    first_backup_finished = threading.Event()
    results = {}
    worker_errors = []
    real_exclusive_lock = _exclusive_backup_lock
    real_snapshot = snapshot_sqlite

    @contextmanager
    def delay_second_lock_until_first_finishes(root):
        if threading.current_thread().name == "second-due-check":
            second_waiting_for_first.set()
            assert first_backup_finished.wait(timeout=3.0)
        with real_exclusive_lock(root):
            yield

    def hold_first_snapshot(source, target):
        if threading.current_thread().name == "first-due-check":
            first_snapshot_started.set()
            assert release_first_snapshot.wait(timeout=3.0)
        return real_snapshot(source, target)

    def run_due_check(name):
        try:
            results[name] = create_if_due(
                backup_context, "scheduled", now=1_700_000_000.0
            )
            if name == "first-due-check":
                first_backup_finished.set()
        except Exception as error:
            worker_errors.append(error)

    with (
        patch(
            "rag.backups.service._exclusive_backup_lock",
            delay_second_lock_until_first_finishes,
        ),
        patch("rag.backups.service.snapshot_sqlite", hold_first_snapshot),
    ):
        workers = [
            threading.Thread(target=run_due_check, args=(name,), name=name)
            for name in ("first-due-check", "second-due-check")
        ]
        workers[0].start()
        assert first_snapshot_started.wait(timeout=1.0)
        workers[1].start()
        assert second_waiting_for_first.wait(timeout=1.0)
        release_first_snapshot.set()
        for worker in workers:
            worker.join(timeout=5.0)

    assert all(not worker.is_alive() for worker in workers)
    assert worker_errors == []
    assert results["first-due-check"] is not None
    assert results["second-due-check"] is None
    assert len(list_verified_backups(backup_context.backup_root)) == 1


def test_due_create_and_cli_fail_closed_while_backup_lock_is_held(
    backup_context, capsys
):
    backup_context.backup_root.mkdir(parents=True, exist_ok=True)
    other_staging = backup_context.backup_root / ".incomplete-other-process"
    other_staging.mkdir()
    (other_staging / "partial.payload").write_bytes(b"unfinished")

    with _exclusive_backup_lock(backup_context.backup_root):
        with pytest.raises(BackupError, match="backup_in_progress"):
            create_backup(backup_context, "manual", now=1_800_000_000.0)
        with pytest.raises(BackupError, match="backup_in_progress"):
            create_if_due(backup_context, "deploy", now=1_800_000_000.0)

        result = backup_cli_main(
            ["create-if-due", "--trigger", "deploy"],
            context_factory=lambda: backup_context,
            now=lambda: 1_800_000_000.0,
        )

    assert result == 1
    assert json.loads(capsys.readouterr().err) == {
        "code": "backup_in_progress",
        "status": "error",
    }
    assert list_verified_backups(backup_context.backup_root) == ()
    assert (backup_context.backup_root / ".backup.lock").is_file()
    assert (other_staging / "partial.payload").read_bytes() == b"unfinished"


def test_scheduler_failure_does_not_end_future_checks():
    calls = []

    def run_once():
        calls.append(len(calls))
        if len(calls) == 1:
            raise BackupError("disk_full")

    scheduler = BackupScheduler(run_once=run_once, check_interval_seconds=0.01)
    scheduler.start()
    try:
        wait_until(lambda: len(calls) >= 2)
    finally:
        scheduler.stop()

    assert len(calls) >= 2


def test_scheduler_stop_joins_background_thread():
    scheduler = BackupScheduler(run_once=lambda: None, check_interval_seconds=3600)

    scheduler.start()
    scheduler.stop(timeout=1.0)

    assert scheduler.is_alive is False


def test_scheduler_start_and_stop_are_idempotent():
    scheduler = BackupScheduler(run_once=lambda: None, check_interval_seconds=3600)

    scheduler.start()
    scheduler.start()
    scheduler.stop(timeout=1.0)
    scheduler.stop(timeout=1.0)

    assert scheduler.is_alive is False


def test_disabled_scheduler_does_not_create_thread(monkeypatch):
    stop_backup_scheduler()
    monkeypatch.setattr(settings, "backup_enabled", False)

    assert start_backup_scheduler() is None
    assert backup_scheduler_is_alive() is False
