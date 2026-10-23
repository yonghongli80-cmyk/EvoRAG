"""Regression coverage for spreadsheet edit activation authorization."""

import hashlib
from dataclasses import replace

import pytest
from openpyxl import Workbook

from rag.document_version_store import final_version_path
from rag.materials.formats import build_default_format_registry
from rag.materials.worker import WorkerServices, process_index_job
from rag.org_auth.repository import OrgRepository
from rag.spreadsheets.edit_models import CellPatch, RequestMeta, SpreadsheetEditRequest
from rag.spreadsheets.edit_service import SpreadsheetEditService
from rag.user_db import UserDB


class MemoryVectorStore:
    def __init__(self):
        self.versions = {}

    def add_units(self, kb_id, chunks, vectors, **kwargs):
        self.versions.setdefault(kwargs["document_version_id"], []).extend(chunks)

    def copy_units_to_version(
        self, kb_id, source_version_id, target_version_id, document_id, version_no,
        unit_keys, dataset_id=None,
    ):
        source = {
            chunk.metadata.get("unit_key"): chunk
            for chunk in self.versions.get(source_version_id, [])
        }
        if not set(unit_keys).issubset(source):
            return 0
        self.versions.setdefault(target_version_id, []).extend(
            replace(
                source[key], document_version_id=target_version_id,
                version_no=version_no, is_current=True,
            )
            for key in unit_keys
        )
        return len(unit_keys)

    def delete_version(self, kb_id, version_id):
        self.versions.pop(version_id, None)

    def mark_current(self, kb_id, document_id, version_id):
        return None


def _make_pending_org_edit(tmp_path, *, actor_is_owner=False, personal=False):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    manager_id = db.create_user("manager", "Password-123!")
    owner_id = manager_id if actor_is_owner else db.create_user("owner", "Password-123!")
    OrgRepository(db).create_unit("TEAM", None, "Team", "team")
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('MANAGER',?,'Manager','Lead')", (manager_id,),
        )
        conn.execute("INSERT INTO org_memberships VALUES('MANAGER','TEAM',1)")
        conn.execute("INSERT INTO org_manager_scopes VALUES('MANAGER','TEAM')")
        if not personal:
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
                "VALUES('org-kb','TEAM','organization')"
            )
    document_id = db.create_document_permission(
        "prices.xlsx", "org-kb", owner_id,
        scope="shared" if personal else "private",
    )
    if personal and not actor_is_owner:
        db.share_document(document_id, manager_id, owner_id, "edit")
    versions_root = tmp_path / "versions"
    structured_root = tmp_path / "datasets"
    source = final_version_path(versions_root, document_id, 1, "prices.xlsx")
    source.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.active.title = "Prices"
    workbook.active.append(["product", "price"])
    workbook.active.append(["Phone", 6800])
    workbook.save(source)
    version_id = db.create_document_version(
        document_id, "prices.xlsx", str(source), hashlib.sha256(source.read_bytes()).hexdigest(), owner_id
    )
    vectors = MemoryVectorStore()
    services = WorkerServices(
        db=db,
        versions_root=versions_root,
        structured_datasets_root=structured_root,
        registry=build_default_format_registry(),
        vector_store=vectors,
        embedder=lambda texts: [[float(len(text))] * 4 for text in texts],
    )
    first = process_index_job(
        {"id": 1, "document_id": document_id, "version_id": version_id}, services
    )
    assert first.status == "done"
    actor = db.get_user_by_id(manager_id)
    accepted = SpreadsheetEditService(db, versions_root, structured_root).save(
        document_id,
        actor,
        SpreadsheetEditRequest(
            base_version_id=version_id,
            changes=(CellPatch(
                sheet_key="sheet_0001", cell="B2", value_type="number", value=6500
            ),),
            reason="price correction",
        ),
        RequestMeta(),
    )
    return db, services, document_id, accepted, manager_id


def test_org_manager_can_activate_spreadsheet_edit(tmp_path):
    db, services, document_id, accepted, _manager_id = _make_pending_org_edit(tmp_path)
    try:
        result = process_index_job(
            {"id": 2, "document_id": document_id, "version_id": accepted.version_id},
            services,
        )

        assert result.status == "done"
        assert db.get_spreadsheet_edit_session(accepted.session_id)["status"] == "active"
    finally:
        db.close()


def test_personal_document_editor_keeps_legacy_share_acl(tmp_path):
    db, services, document_id, accepted, _manager_id = _make_pending_org_edit(
        tmp_path, personal=True
    )
    try:
        result = process_index_job(
            {"id": 5, "document_id": document_id, "version_id": accepted.version_id},
            services,
        )

        assert result.status == "done"
        assert db.get_spreadsheet_edit_session(accepted.session_id)["status"] == "active"
    finally:
        db.close()


@pytest.mark.parametrize("actor_is_owner", [False, True])
def test_revoked_org_manager_cannot_activate_spreadsheet_edit(
    tmp_path, actor_is_owner
):
    db, services, document_id, accepted, manager_id = _make_pending_org_edit(
        tmp_path, actor_is_owner=actor_is_owner
    )
    try:
        with db.locked_transaction() as conn:
            conn.execute(
                "DELETE FROM org_manager_scopes WHERE person_code='MANAGER'"
            )
            conn.execute(
                "DELETE FROM org_memberships WHERE person_code='MANAGER'"
            )

        result = process_index_job(
            {"id": 3, "document_id": document_id, "version_id": accepted.version_id},
            services,
        )

        assert result.status == "cancelled"
        assert result.error_code == "permission_revoked"
        assert db.get_spreadsheet_edit_session(accepted.session_id)["status"] == "cancelled"
        base_version_id = db.get_spreadsheet_edit_session(accepted.session_id)["base_version_id"]
        assert db.get_current_document_version(document_id)["id"] == base_version_id
    finally:
        db.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "person_inactive",
        "unit_inactive",
        "policy_inactive",
        "tombstone",
        "protected",
    ],
)
def test_inactive_or_unavailable_org_edit_cannot_activate(
    tmp_path, mutation
):
    db, services, document_id, accepted, _manager_id = _make_pending_org_edit(tmp_path)
    try:
        with db.locked_transaction() as conn:
            if mutation == "person_inactive":
                conn.execute("UPDATE org_people SET active=0 WHERE person_code='MANAGER'")
            elif mutation == "unit_inactive":
                conn.execute("UPDATE org_units SET active=0 WHERE code='TEAM'")
            elif mutation == "policy_inactive":
                conn.execute("UPDATE kb_org_policies SET active=0 WHERE kb_id='org-kb'")
            elif mutation == "tombstone":
                conn.execute(
                    "UPDATE document_permissions SET deleted_at=1 WHERE id=?",
                    (document_id,),
                )
            else:
                conn.execute(
                    "UPDATE document_permissions SET protected=1 WHERE id=?",
                    (document_id,),
                )

        result = process_index_job(
            {"id": 4, "document_id": document_id, "version_id": accepted.version_id},
            services,
        )

        assert result.status == "cancelled"
        assert result.error_code == "permission_revoked"
        assert db.get_spreadsheet_edit_session(accepted.session_id)["status"] == "cancelled"
        base_version_id = db.get_spreadsheet_edit_session(accepted.session_id)["base_version_id"]
        assert db.get_current_document_version(document_id)["id"] == base_version_id
    finally:
        db.close()
