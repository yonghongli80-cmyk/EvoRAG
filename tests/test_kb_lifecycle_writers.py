from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from rag.document_sources.upload import UploadDocumentSource
from rag.document_version_store import stage_version
from rag.document_versions import (
    ingest_document_version,
    migrate_existing_document_versions,
    migrate_staged_document_versions,
)
from rag.kb_lifecycle import KBDeleting, kb_mutation
from rag.org_auth.migration import migrate_legacy_knowledge_bases
from rag.org_auth.seed import seed_organization
from rag.spreadsheets.edit_models import (
    CellPatch,
    RequestMeta,
    SpreadsheetEditRequest,
    SpreadsheetRestoreRequest,
    SpreadsheetStructureEditRequest,
)
from rag.spreadsheets.edit_service import SpreadsheetEditService
from rag.spreadsheets.migration import migrate_existing_spreadsheets
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset
from rag.user_db import UserDB, VersionConflict


def _tombstone(db: UserDB, kb_id: str) -> None:
    with kb_mutation(db, kb_id, mode="delete") as permit:
        permit.begin_delete()


def _workbook(path: Path, value: str = "v1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.title = "Prices"
    book.active.append(["product", "price"])
    book.active.append(["Phone", value])
    book.save(path)
    return path


def _spreadsheet(db: UserDB, root: Path, kb_id: str = "kb_sales"):
    owner_id = db.create_user("owner", "Password-123!")
    db.create_kb_metadata(kb_id, "Sales")
    owner = db.get_user_by_id(owner_id)
    document_id = db.create_document_permission("prices.xlsx", kb_id, owner_id)
    source = _workbook(root / "source.xlsx")
    version_id = db.create_document_version(
        document_id,
        "prices.xlsx",
        str(source),
        hashlib.sha256(source.read_bytes()).hexdigest(),
        owner_id,
    )
    dataset_id = db.create_structured_dataset(
        document_id, version_id, "xlsx", "row_groups"
    )
    dataset_path = resolve_dataset_path(root.parent / "datasets", document_id, version_id)
    write_dataset(dataset_path, parse_spreadsheet(source, "xlsx", None))
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id=kb_id,
        filename="prices.xlsx",
    )
    return owner, document_id, version_id, source


def test_common_ingest_does_not_reserve_permission_or_file_after_delete_intent(
    tmp_path,
):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    manager = SimpleNamespace(add_document=lambda *args, **kwargs: 1)
    db.create_kb_metadata("kb_ingest", "Ingest")
    owner = db.create_user("owner", "Password-123!")
    _tombstone(second, "kb_ingest")
    source = UploadDocumentSource.from_bytes("notes.txt", b"body")

    try:
        with pytest.raises(KBDeleting):
            ingest_document_version(
                db,
                manager,
                source,
                "kb_ingest",
                {"id": owner},
                "reject",
                tmp_path / "versions",
            )

        assert db.get_document_permission("notes.txt", "kb_ingest") is None
        assert not list((tmp_path / "versions").rglob("*"))
    finally:
        second.close()
        db.close()


def test_batch_import_does_not_write_after_delete_intent(tmp_path, monkeypatch):
    from rag.batch_importer import BatchImporter

    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("kb_batch", "Batch")
    owner_id = db.create_user("owner", "Password-123!")
    owner = db.get_user_by_id(owner_id)
    source = tmp_path / "qa.csv"
    source.write_text("问题,答案\nQ,A\n", encoding="utf-8")
    _tombstone(second, "kb_batch")

    def forbidden_model_call(_texts):
        raise AssertionError("embedding must not run after delete intent")

    monkeypatch.setattr("rag.embedder.embed", forbidden_model_call)
    try:
        with pytest.raises(KBDeleting):
            BatchImporter().import_file(
                str(source),
                kb_id="kb_batch",
                user=owner,
                user_db=db,
            )

        assert db.get_document_permission("qa.csv", "kb_batch") is None
    finally:
        second.close()
        db.close()


def test_document_version_staging_can_be_scoped_to_its_document(tmp_path):
    source = UploadDocumentSource.from_bytes("notes.txt", b"same source key")
    root = tmp_path / "versions"

    first = stage_version(root, source, document_id=11)
    second = stage_version(root, source, document_id=22)

    assert first.parent == root / "staging" / "11"
    assert second.parent == root / "staging" / "22"
    assert first.read_bytes() == second.read_bytes() == b"same source key"


def test_repository_document_writers_reject_a_tombstoned_kb(tmp_path):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("kb_repo", "Repository")
    owner_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("legacy.pdf", "kb_repo", owner_id)
    db.create_kb_document_pending("kb_repo", "legacy.pdf")
    staged_version_id = db.create_document_version(
        document_id, "legacy.pdf", "legacy.pdf", "hash", owner_id
    )
    _tombstone(second, "kb_repo")

    try:
        with pytest.raises(KBDeleting):
            db.create_document_permission("blocked.pdf", "kb_repo", owner_id)
        with pytest.raises(KBDeleting):
            db.create_document_version(
                document_id, "legacy.pdf", "legacy.pdf", "hash", owner_id
            )
        with pytest.raises(KBDeleting):
            db.activate_document_version(
                document_id, staged_version_id, "kb_repo", "legacy.pdf"
            )
        with pytest.raises(KBDeleting):
            db.finish_kb_document(document_id, "legacy.pdf", "", "", 1)
        with pytest.raises(VersionConflict, match="kb document no longer exists"):
            db.finish_kb_document(99999, "missing.pdf", "", "", 1)

        assert db.get_document_permission("blocked.pdf", "kb_repo") is None
        assert [item["status"] for item in db.list_document_versions(document_id)] == [
            "staging"
        ]
        assert db.get_kb_document("kb_repo", "legacy.pdf")["status"] == "pending"
    finally:
        second.close()
        db.close()


def test_version_reservation_rejects_missing_document_permission(tmp_path):
    db = UserDB(tmp_path / "users.db")
    owner_id = db.create_user("owner", "Password-123!")

    try:
        with pytest.raises(VersionConflict, match="document permission no longer exists"):
            db.create_document_version(
                99999, "missing.pdf", "missing.pdf", "hash", owner_id
            )

        assert db.list_document_versions(99999) == []
    finally:
        db.close()


def test_version_activation_rejects_missing_document_permission_without_mutating(
    tmp_path,
):
    db = UserDB(tmp_path / "users.db")
    owner_id = db.create_user("owner", "Password-123!")
    with db.cm.write_connection() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        version_id = conn.execute(
            "INSERT INTO document_versions "
            "(document_id, version_no, original_filename, file_path, content_hash, uploaded_by, status) "
            "VALUES (99999, 1, 'orphan.pdf', 'orphan.pdf', 'hash', ?, 'staging')",
            (owner_id,),
        ).lastrowid
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

    try:
        with pytest.raises(VersionConflict, match="document permission no longer exists"):
            db.activate_document_version(
                99999, version_id, "kb_missing", "orphan.pdf"
            )

        assert db.get_document_version_by_id(version_id)["status"] == "staging"
    finally:
        db.close()


def test_document_version_startup_migration_skips_tombstoned_kb(tmp_path):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("kb_legacy", "Legacy")
    owner_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("legacy.pdf", "kb_legacy", owner_id)
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "legacy.pdf").write_bytes(b"legacy bytes")
    versions_root = tmp_path / "versions"
    _tombstone(second, "kb_legacy")

    try:
        migrate_existing_document_versions(db, versions_root, upload_root)

        assert db.list_document_versions(document_id) == []
        assert not list(versions_root.rglob("*"))
    finally:
        second.close()
        db.close()


def test_staged_version_startup_migration_does_not_move_tombstoned_file(tmp_path):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("kb_legacy", "Legacy")
    owner_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("legacy.pdf", "kb_legacy", owner_id)
    versions_root = tmp_path / "versions"
    staged = versions_root / "staging" / "old-stage.pdf"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"legacy bytes")
    version_id = db.create_document_version(
        document_id, "legacy.pdf", str(staged), "hash", owner_id
    )
    db.activate_document_version(document_id, version_id, "kb_legacy", "legacy.pdf")
    _tombstone(second, "kb_legacy")

    try:
        migrate_staged_document_versions(db, versions_root)

        version = db.get_document_version_by_id(version_id)
        assert version["file_path"] == str(staged)
        assert staged.read_bytes() == b"legacy bytes"
        assert not (versions_root / str(document_id) / "1" / "legacy.pdf").exists()
    finally:
        second.close()
        db.close()


def test_spreadsheet_startup_migration_does_not_create_dataset_for_tombstone(
    tmp_path,
):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    owner = db.create_user("owner", "Password-123!")
    db.create_kb_metadata("kb_sales", "Sales")
    document_id = db.create_document_permission("sales.xlsx", "kb_sales", owner)
    source = _workbook(tmp_path / "sales.xlsx")
    version_id = db.create_document_version(
        document_id, "sales.xlsx", str(source), "hash", owner
    )
    db.activate_document_version(document_id, version_id, "kb_sales", "sales.xlsx")
    datasets_root = tmp_path / "datasets"
    _tombstone(second, "kb_sales")

    try:
        migrate_existing_spreadsheets(db, datasets_root)

        assert db.get_structured_dataset_for_version(version_id) is None
        assert not datasets_root.exists()
    finally:
        second.close()
        db.close()


def test_spreadsheet_edit_does_not_reserve_or_stage_after_delete_intent(tmp_path):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    versions_root = tmp_path / "versions"
    owner, document_id, version_id, _ = _spreadsheet(
        db, versions_root, "kb_sales"
    )
    _tombstone(second, "kb_sales")
    service = SpreadsheetEditService(
        db,
        versions_root,
        tmp_path / "datasets",
        enqueue=lambda *args: 1,
    )

    try:
        with pytest.raises(KBDeleting):
            service.save(
                document_id,
                owner,
                SpreadsheetEditRequest(
                    base_version_id=version_id,
                    changes=(
                        CellPatch(
                            sheet_key="sheet_0001",
                            cell="B2",
                            value_type="text",
                            value="new",
                        ),
                    ),
                ),
                RequestMeta(),
            )

        assert len(db.list_document_versions(document_id)) == 1
        assert not list((versions_root / "staging").glob("*"))
    finally:
        second.close()
        db.close()


def test_spreadsheet_restore_does_not_reserve_or_stage_after_delete_intent(
    tmp_path,
):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    versions_root = tmp_path / "versions"
    owner, document_id, version_id, source = _spreadsheet(
        db, versions_root, "kb_sales"
    )
    old = versions_root / str(document_id) / "1" / "prices.xlsx"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(source.read_bytes())
    old_version = db.create_document_version(
        document_id, "prices.xlsx", str(old), "old-hash", owner["id"]
    )
    db.set_version_status(old_version, "historical")
    _tombstone(second, "kb_sales")
    service = SpreadsheetEditService(
        db,
        versions_root,
        tmp_path / "datasets",
        enqueue=lambda *args: 1,
    )

    try:
        with pytest.raises(KBDeleting):
            service.restore(
                document_id,
                old_version,
                owner,
                SpreadsheetRestoreRequest(base_version_id=version_id),
                RequestMeta(),
            )

        assert len(db.list_document_versions(document_id)) == 2
        assert not list((versions_root / "staging").glob("*"))
    finally:
        second.close()
        db.close()


def test_spreadsheet_structure_edit_does_not_reserve_after_delete_intent(
    tmp_path,
):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    versions_root = tmp_path / "versions"
    owner, document_id, version_id, _ = _spreadsheet(
        db, versions_root, "kb_sales"
    )
    _tombstone(second, "kb_sales")
    service = SpreadsheetEditService(
        db,
        versions_root,
        tmp_path / "datasets",
        enqueue=lambda *args: 1,
    )

    try:
        with pytest.raises(KBDeleting):
            service.save_structure(
                document_id,
                owner,
                SpreadsheetStructureEditRequest(
                    base_version_id=version_id,
                    action="insert_rows",
                    sheet_key="sheet_0001",
                    index=2,
                ),
                RequestMeta(),
            )

        assert len(db.list_document_versions(document_id)) == 1
        assert not list((versions_root / "staging").glob("**/*"))
    finally:
        second.close()
        db.close()


def test_spreadsheet_edit_api_maps_lifecycle_error_to_conflict(
    tmp_path, monkeypatch
):
    from rag.spreadsheets import api

    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    versions_root = tmp_path / "versions"
    owner, document_id, version_id, _ = _spreadsheet(
        db, versions_root, "kb_sales"
    )
    _tombstone(second, "kb_sales")
    service = SpreadsheetEditService(
        db,
        versions_root,
        tmp_path / "datasets",
        enqueue=lambda *args: 1,
    )
    monkeypatch.setattr(api, "_edit_service", lambda _request: service)
    request = SimpleNamespace(
        client=None,
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(user_db=db)),
    )
    response = SimpleNamespace(status_code=0)

    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                api.create_spreadsheet_edit(
                    document_id,
                    SpreadsheetEditRequest(
                        base_version_id=version_id,
                        changes=(
                            CellPatch(
                                sheet_key="sheet_0001",
                                cell="B2",
                                value_type="text",
                                value="new",
                            ),
                        ),
                    ),
                    request,
                    response,
                    owner,
                )
            )

        assert error.value.status_code == 409
        assert error.value.headers["X-KB-Lifecycle-Error"] == "knowledge_base_deleting"
    finally:
        second.close()
        db.close()


def test_legacy_policy_migration_leaves_tombstoned_kb_unmapped(tmp_path):
    db = UserDB(tmp_path / "users.db")
    second = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("kb_legacy", "Legacy")
    _tombstone(second, "kb_legacy")

    try:
        with pytest.raises(KBDeleting):
            migrate_legacy_knowledge_bases(db)

        with db.locked_read() as conn:
            policy = conn.execute(
                "SELECT 1 FROM kb_org_policies WHERE kb_id='kb_legacy'"
            ).fetchone()
        assert policy is None
    finally:
        second.close()
        db.close()


def test_org_seed_rolls_back_when_manifest_kb_has_tombstone(tmp_path, monkeypatch):
    from rag.org_auth.seed_manifest import KnowledgeBaseSeed, SeedManifest

    db = UserDB(tmp_path / "users.db")
    kb_id = "kb_seeded"
    with db.locked_transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO kb_lifecycle(kb_id,state,generation,requested_at) "
            "VALUES(?,'deleting',1,0)",
            (kb_id,),
        )
    manifest = SeedManifest(
        units=(),
        people=(),
        memberships=(),
        manager_scopes=(),
        qualifications=(),
        knowledge_bases=(
            KnowledgeBaseSeed(kb_id, "Seeded", None, "organization", None, ()),
        ),
    )
    monkeypatch.setattr("rag.org_auth.seed.build_seed_manifest", lambda: manifest)

    try:
        with pytest.raises(KBDeleting):
            seed_organization(db, metadata_only=True)

        assert db.get_kb_metadata(kb_id) is None
        with db.locked_read() as conn:
            assert conn.execute(
                "SELECT 1 FROM kb_org_policies WHERE kb_id=?", (kb_id,)
            ).fetchone() is None
        assert db.get_user_by_username("demo001") is None
    finally:
        db.close()
