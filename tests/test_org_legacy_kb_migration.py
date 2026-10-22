"""B-02: legacy knowledge bases are migrated on startup and fail closed.

The 2026-08-20 organization plan §7 states that, during migration, legacy
knowledge bases are visible to company administrators only.  Before this guard
existed, ``require_kb_view``/``require_kb_edit`` fell back to the personal
``kb_metadata.scope`` ACL, so an old ``public`` knowledge base stayed readable
by every signed-in account by id.  An unmigrated KB now keeps only its legacy
owner and administrators; ``public``/``shared`` scopes and ``kb_shares`` grants
no longer authorize anyone.  These tests pin that contract and the
unconditional startup migration.
"""

import asyncio
import logging

import pytest
from fastapi import HTTPException

from rag.api_security import require_kb_edit, require_kb_view
from rag.org_auth.migration import (
    MigrationReport,
    count_unmigrated_knowledge_bases,
    migrate_legacy_knowledge_bases,
)
from rag.org_auth.repository import OrgRepository
from rag.user_db import UserDB


def _create_legacy_kb(tmp_path, scope: str = "public") -> UserDB:
    db = UserDB(str(tmp_path / "legacy.db"))
    owner = db.create_user("legacy-owner", "Password1")
    db.create_kb_metadata("legacy-kb", "旧知识库", owner, scope)
    return db


def test_policy_less_public_kb_never_falls_back_to_the_public_acl(tmp_path):
    db = _create_legacy_kb(tmp_path, scope="public")
    try:
        metadata = db.get_kb_metadata("legacy-kb")
        employee_id = db.create_user("employee", "Password2")
        shared_id = db.create_user("shared", "Password3")
        db.share_kb("legacy-kb", shared_id, metadata["owner_id"], "view")
        admin_id = db.create_user("root", "Password4")
        db.set_user_admin(admin_id, True)
        employee = db.get_user_by_id(employee_id)
        shared = db.get_user_by_id(shared_id)
        admin = db.get_user_by_id(admin_id)
        owner = db.get_user_by_id(metadata["owner_id"])
        assert employee["is_admin"] is False
        assert OrgRepository(db).get_kb_policy("legacy-kb") is None

        # No legacy "public scope => every signed-in account" fallback, and a
        # legacy share no longer authorizes a non-owner.
        with pytest.raises(HTTPException) as view_denied:
            require_kb_view(db, "legacy-kb", employee)
        assert view_denied.value.status_code == 403
        with pytest.raises(HTTPException) as shared_denied:
            require_kb_view(db, "legacy-kb", shared)
        assert shared_denied.value.status_code == 403
        with pytest.raises(HTTPException) as edit_denied:
            require_kb_edit(db, "legacy-kb", employee)
        assert edit_denied.value.status_code == 403

        # A "shared" scope plus an edit grant still does not authorize anyone
        # but the owner until the KB is migrated to an organization.
        db.create_kb_metadata("legacy-shared", "旧共享库", owner["id"], "shared")
        db.share_kb("legacy-shared", shared_id, owner["id"], "edit")
        with pytest.raises(HTTPException) as shared_scope_denied:
            require_kb_view(db, "legacy-shared", shared)
        assert shared_scope_denied.value.status_code == 403
        with pytest.raises(HTTPException) as shared_edit_denied:
            require_kb_edit(db, "legacy-shared", shared)
        assert shared_edit_denied.value.status_code == 403

        # The legacy owner and administrators keep the access they had before.
        assert require_kb_view(db, "legacy-kb", owner)["kb_id"] == "legacy-kb"
        assert require_kb_edit(db, "legacy-kb", owner)["kb_id"] == "legacy-kb"
        assert require_kb_view(db, "legacy-kb", admin)["kb_id"] == "legacy-kb"
        assert require_kb_edit(db, "legacy-kb", admin)["kb_id"] == "legacy-kb"
    finally:
        db.close()


def test_migrated_legacy_kb_stays_admin_only_until_an_org_is_assigned(tmp_path):
    db = _create_legacy_kb(tmp_path)
    try:
        owner = db.get_user_by_id(db.get_kb_metadata("legacy-kb")["owner_id"])
        employee = db.get_user_by_id(db.create_user("employee", "Password2"))
        admin_id = db.create_user("root", "Password3")
        db.set_user_admin(admin_id, True)
        admin = db.get_user_by_id(admin_id)

        migrate_legacy_knowledge_bases(db)

        assert OrgRepository(db).get_kb_policy("legacy-kb")["kb_type"] == "unassigned"
        with pytest.raises(HTTPException) as denied:
            require_kb_view(db, "legacy-kb", employee)
        assert denied.value.status_code == 404
        # Migration retires even the owner's personal ACL: only administrators
        # can reach an ``unassigned`` KB until it is explicitly assigned.
        with pytest.raises(HTTPException) as owner_denied:
            require_kb_view(db, "legacy-kb", owner)
        assert owner_denied.value.status_code == 404
        migrated = require_kb_view(db, "legacy-kb", admin)
        assert migrated["kb_type"] == "unassigned"
        assert migrated["effective_role"] == "administer"
    finally:
        db.close()


def test_migration_reports_the_remaining_unmigrated_counter(tmp_path):
    db = _create_legacy_kb(tmp_path)
    try:
        assert count_unmigrated_knowledge_bases(db) == 1

        report = migrate_legacy_knowledge_bases(db)

        assert report.migrated == 1
        assert report.unmigrated == 0
        assert count_unmigrated_knowledge_bases(db) == 0
        # Idempotent: a second startup migrates nothing and finds no leftovers.
        second = migrate_legacy_knowledge_bases(db)
        assert second.migrated == 0
        assert second.unmigrated == 0
    finally:
        db.close()


def test_lifespan_runs_the_legacy_kb_migration_unconditionally(monkeypatch):
    import rag.api as api

    calls: list[bool] = []
    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: calls.append(True))
    for name in (
        "auto_index_on_startup",
        "_run_staged_version_migration",
        "start_backup_scheduler",
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

    assert calls == [True]


def test_startup_migration_logs_the_unmigrated_counter(monkeypatch, caplog):
    import rag.api as api
    from rag.org_auth import migration

    monkeypatch.setattr(
        migration,
        "migrate_legacy_knowledge_bases",
        lambda db: MigrationReport(migrated=0, unmigrated=2),
    )

    with caplog.at_level(logging.INFO, logger="rag.api"):
        api._run_legacy_kb_migration()

    assert any("remaining=2" in record.getMessage() for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)
