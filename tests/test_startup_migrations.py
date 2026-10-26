"""Startup hooks: legacy KB migration and stale ``staging/`` version repair.

The D-01/W3 remediation added ``migrate_staged_document_versions``; the API
lifespan owns the one startup call, and it must happen before the backup
scheduler so a deployment cannot open a backup window while committed rows
still point into the excluded ``staging`` directory.
"""

import asyncio
from pathlib import Path

import pytest

from rag.document_version_store import final_version_path


def _install_lifespan_spies(monkeypatch, order: list[str], api):
    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: order.append("legacy_kb"))
    monkeypatch.setattr(api, "auto_index_on_startup", lambda: order.append("index"))
    monkeypatch.setattr(api, "_run_staged_version_migration", lambda: order.append("staged_versions"))
    monkeypatch.setattr(api, "start_backup_scheduler", lambda: order.append("backups"))
    for name in (
        "start_evolution_worker",
        "restore_question_set_rollout",
        "stop_backup_scheduler",
        "stop_evolution_worker",
        "close_pipeline_processors",
    ):
        monkeypatch.setattr(api, name, lambda: None)


def test_lifespan_promotes_staged_versions_before_the_backup_scheduler(monkeypatch):
    import rag.api as api

    order: list[str] = []
    _install_lifespan_spies(monkeypatch, order, api)

    async def _enter() -> None:
        async with api.lifespan(api.app):
            pass

    asyncio.run(_enter())

    assert order == ["legacy_kb", "index", "staged_versions", "backups"]


def test_lifespan_runs_lifecycle_migrations_in_sync_workers_before_backups(
    monkeypatch,
):
    import rag.api as api

    order: list[str] = []
    in_sync_worker: dict[str, bool] = {}

    def record(name: str) -> None:
        order.append(name)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        in_sync_worker[name] = current_task is None

    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: record("legacy_kb"))
    monkeypatch.setattr(api, "auto_index_on_startup", lambda: record("index"))
    monkeypatch.setattr(
        api,
        "_run_staged_version_migration",
        lambda: record("staged_versions"),
    )
    monkeypatch.setattr(api, "start_backup_scheduler", lambda: record("backups"))
    for name in (
        "start_evolution_worker",
        "restore_question_set_rollout",
        "stop_backup_scheduler",
        "stop_evolution_worker",
        "close_pipeline_processors",
    ):
        monkeypatch.setattr(api, name, lambda: None)

    async def _enter() -> None:
        async with api.lifespan(api.app):
            pass

    asyncio.run(_enter())

    assert order == ["legacy_kb", "index", "staged_versions", "backups"]
    assert in_sync_worker["legacy_kb"] is False
    assert in_sync_worker["index"] is True
    assert in_sync_worker["staged_versions"] is True
    assert in_sync_worker["backups"] is False


def test_startup_repairs_committed_versions_left_in_staging(isolated_api, make_user):
    import rag.api as api

    user, _ = make_user("staged-owner")
    db = api.app.state.user_db
    versions_root = Path(api.app.state.document_versions_root)
    document_id = db.create_document_permission("legacy.txt", "rag_docs", user["id"])
    staged = versions_root / "staging" / "legacy-1-legacy.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"legacy body")
    version_id = db.create_document_version(document_id, "legacy.txt", str(staged), "legacy-hash", user["id"])
    db.activate_document_version(document_id, version_id, "rag_docs", "legacy.txt")

    api._run_staged_version_migration()

    migrated = db.get_document_version_by_id(version_id)
    expected = final_version_path(versions_root, document_id, 1, "legacy.txt")
    assert Path(migrated["file_path"]) == expected
    assert expected.read_bytes() == b"legacy body"
    assert not staged.exists()
    assert not expected.is_relative_to(versions_root / "staging")


def _install_late_startup_spies(monkeypatch, api, calls, *, restore=None):
    """Keep pre-thread work inert while exercising only late lifecycle hooks."""
    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: calls.append("legacy"))
    monkeypatch.setattr(api, "auto_index_on_startup", lambda: calls.append("index"))
    monkeypatch.setattr(api, "_run_staged_version_migration", lambda: calls.append("staged"))
    monkeypatch.setattr(api, "start_backup_scheduler", lambda: calls.append("start-backup"))
    monkeypatch.setattr(api, "start_evolution_worker", lambda: calls.append("start-evolution"))
    monkeypatch.setattr(
        api,
        "restore_question_set_rollout",
        restore or (lambda: calls.append("restore")),
    )


def _enter_lifespan(api):
    async def _enter():
        async with api.lifespan(api.app):
            pass

    asyncio.run(_enter())


def test_lifespan_cleans_late_resources_when_rollout_restore_fails(monkeypatch):
    import rag.api as api

    calls = []
    primary = RuntimeError("rollout restore failed")

    def fail_restore():
        calls.append("restore")
        raise primary

    _install_late_startup_spies(monkeypatch, api, calls, restore=fail_restore)
    monkeypatch.setattr(api, "stop_backup_scheduler", lambda: calls.append("stop-backup"))
    monkeypatch.setattr(api, "stop_evolution_worker", lambda: calls.append("stop-evolution"))
    monkeypatch.setattr(api, "close_pipeline_processors", lambda: calls.append("close-processors"))

    with pytest.raises(RuntimeError) as raised:
        _enter_lifespan(api)

    assert raised.value is primary
    assert calls == [
        "legacy",
        "index",
        "staged",
        "start-backup",
        "start-evolution",
        "restore",
        "stop-backup",
        "stop-evolution",
        "close-processors",
    ]


def test_lifespan_cleanup_errors_do_not_mask_primary_or_skip_later_cleanup(monkeypatch):
    import rag.api as api

    calls = []
    primary = RuntimeError("startup failed")
    cleanup_errors = [RuntimeError("backup stop failed"), RuntimeError("worker stop failed")]

    def fail_restore():
        calls.append("restore")
        raise primary

    def fail_cleanup(name, error=None):
        def _cleanup():
            calls.append(name)
            if error is not None:
                raise error

        return _cleanup

    _install_late_startup_spies(monkeypatch, api, calls, restore=fail_restore)
    monkeypatch.setattr(api, "stop_backup_scheduler", fail_cleanup("stop-backup", cleanup_errors[0]))
    monkeypatch.setattr(api, "stop_evolution_worker", fail_cleanup("stop-evolution", cleanup_errors[1]))
    monkeypatch.setattr(api, "close_pipeline_processors", fail_cleanup("close-processors"))

    with pytest.raises(RuntimeError) as raised:
        _enter_lifespan(api)

    assert raised.value is primary
    assert calls[-3:] == ["stop-backup", "stop-evolution", "close-processors"]


def test_lifespan_cleanup_errors_attempt_every_hook_and_raise_first_without_primary(monkeypatch):
    import rag.api as api

    calls = []
    first_cleanup_error = RuntimeError("backup stop failed")

    def fail_cleanup(name, error=None):
        def _cleanup():
            calls.append(name)
            if error is not None:
                raise error

        return _cleanup

    _install_late_startup_spies(monkeypatch, api, calls)
    monkeypatch.setattr(api, "stop_backup_scheduler", fail_cleanup("stop-backup", first_cleanup_error))
    monkeypatch.setattr(
        api, "stop_evolution_worker", fail_cleanup("stop-evolution", RuntimeError("worker stop failed"))
    )
    monkeypatch.setattr(api, "close_pipeline_processors", fail_cleanup("close-processors"))

    with pytest.raises(RuntimeError) as raised:
        _enter_lifespan(api)

    assert raised.value is first_cleanup_error
    assert calls[-3:] == ["stop-backup", "stop-evolution", "close-processors"]


def test_lifespan_preserves_cancellation_during_rollout_restore(monkeypatch):
    import rag.api as api

    calls = []
    cancellation = asyncio.CancelledError("startup cancelled")

    def cancel_restore():
        calls.append("restore")
        raise cancellation

    _install_late_startup_spies(monkeypatch, api, calls, restore=cancel_restore)
    monkeypatch.setattr(api, "stop_backup_scheduler", lambda: calls.append("stop-backup"))
    monkeypatch.setattr(api, "stop_evolution_worker", lambda: calls.append("stop-evolution"))
    monkeypatch.setattr(api, "close_pipeline_processors", lambda: calls.append("close-processors"))

    with pytest.raises(asyncio.CancelledError) as raised:
        _enter_lifespan(api)

    assert raised.value is cancellation
    assert calls[-3:] == ["stop-backup", "stop-evolution", "close-processors"]
