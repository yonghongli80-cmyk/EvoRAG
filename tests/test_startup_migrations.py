"""Startup hooks: legacy KB migration and stale ``staging/`` version repair.

The D-01/W3 remediation added ``migrate_staged_document_versions``; the API
lifespan owns the one startup call, and it must happen before the backup
scheduler so a deployment cannot open a backup window while committed rows
still point into the excluded ``staging`` directory.
"""

import asyncio
from pathlib import Path

from rag.document_version_store import final_version_path


def _install_lifespan_spies(monkeypatch, order: list[str], api):
    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: order.append("legacy_kb"))
    monkeypatch.setattr(api, "auto_index_on_startup", lambda: order.append("index"))
    monkeypatch.setattr(
        api, "_run_staged_version_migration", lambda: order.append("staged_versions")
    )
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


def test_startup_repairs_committed_versions_left_in_staging(isolated_api, make_user):
    import rag.api as api

    user, _ = make_user("staged-owner")
    db = api.app.state.user_db
    versions_root = Path(api.app.state.document_versions_root)
    document_id = db.create_document_permission("legacy.txt", "rag_docs", user["id"])
    staged = versions_root / "staging" / "legacy-1-legacy.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"legacy body")
    version_id = db.create_document_version(
        document_id, "legacy.txt", str(staged), "legacy-hash", user["id"]
    )
    db.activate_document_version(document_id, version_id, "rag_docs", "legacy.txt")

    api._run_staged_version_migration()

    migrated = db.get_document_version_by_id(version_id)
    expected = final_version_path(versions_root, document_id, 1, "legacy.txt")
    assert Path(migrated["file_path"]) == expected
    assert expected.read_bytes() == b"legacy body"
    assert not staged.exists()
    assert not expected.is_relative_to(versions_root / "staging")
