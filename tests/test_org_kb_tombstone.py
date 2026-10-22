"""D-02: deletion tombstones are enforced on organization knowledge bases.

``check_doc_permission`` rejects ``deleted_at IS NOT NULL`` for the personal ACL
path, but the organization branch of ``require_doc_view``/``require_doc_edit``
returned the document record without looking at the tombstone.  Row data then
stayed readable after deletion because the row endpoint only inspected
``structured_datasets.status`` and never the version status.
"""

import hashlib

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from rag.api_security import require_doc_edit, require_doc_view
from rag.document_version_store import final_version_path
from rag.materials.recycle import delete_to_recycle
from rag.org_auth.repository import OrgRepository
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset
from rag.user_db import UserDB


def _seed_org_kb(db: UserDB, member_id: int, kb_id: str = "kb_org") -> None:
    repo = OrgRepository(db)
    repo.create_unit("TEAM", None, "协作组", "team")
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P1',?,'甲','专员')",
            (member_id,),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
        conn.execute("INSERT INTO org_manager_scopes VALUES('P1','TEAM')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES(?,'TEAM','organization')",
            (kb_id,),
        )


def test_tombstoned_document_in_an_org_kb_is_a_404_for_members(tmp_path):
    db = UserDB(str(tmp_path / "org-tombstone.db"))
    db.create_material_schema()
    try:
        member_id = db.create_user("manager", "Password1")
        _seed_org_kb(db, member_id)
        document_id = db.create_document_permission(
            "budget.xlsx", "kb_org", member_id, scope="private"
        )
        member = db.get_user_by_id(member_id)

        # A manager passes both gates while the document is alive.
        assert require_doc_view(db, "budget.xlsx", "kb_org", member)["id"] == document_id
        require_doc_edit(db, "budget.xlsx", "kb_org", member)

        delete_to_recycle(db, document_id, member, organization_authorized=True)

        with pytest.raises(HTTPException) as view_denied:
            require_doc_view(db, "budget.xlsx", "kb_org", member)
        assert view_denied.value.status_code == 404
        with pytest.raises(HTTPException) as edit_denied:
            require_doc_edit(db, "budget.xlsx", "kb_org", member)
        assert edit_denied.value.status_code == 404
    finally:
        db.close()


def test_tombstoned_org_document_is_hidden_from_administrators_too(tmp_path):
    """Match the personal ACL path: a tombstone outranks the admin bypass."""
    db = UserDB(str(tmp_path / "org-tombstone-admin.db"))
    db.create_material_schema()
    try:
        admin_id = db.create_user("root", "Password1")
        db.set_user_admin(admin_id, True)
        _seed_org_kb(db, admin_id)
        document_id = db.create_document_permission(
            "budget.xlsx", "kb_org", admin_id, scope="private"
        )
        admin = db.get_user_by_id(admin_id)

        delete_to_recycle(db, document_id, admin, organization_authorized=True)

        with pytest.raises(HTTPException) as denied:
            require_doc_view(db, "budget.xlsx", "kb_org", admin)
        assert denied.value.status_code == 404
    finally:
        db.close()


def _seed_org_spreadsheet(client, member: dict):
    db = client.app.state.user_db
    _seed_org_kb(db, member["id"])
    document_id = db.create_document_permission("org.xlsx", "kb_org", member["id"])
    root = client.app.state.document_versions_root
    source = final_version_path(root, document_id, 1, "org.xlsx")
    source.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.title = "Plan"
    book.active.append(["region", "amount"])
    book.active.append(["east", 42])
    book.save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    version_id = db.create_document_version(
        document_id, "org.xlsx", str(source), digest, member["id"]
    )
    dataset_id = db.create_structured_dataset(
        document_id, version_id, "xlsx", "row_groups"
    )
    target = resolve_dataset_path(
        client.app.state.structured_datasets_root, document_id, version_id
    )
    write_dataset(target, parse_spreadsheet(source, "xlsx", None))
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id="kb_org",
        filename="org.xlsx",
    )
    return document_id, version_id


def test_org_spreadsheet_rows_are_404_after_the_document_is_deleted(
    isolated_api, make_user
):
    member, headers = make_user("org-member")
    document_id, version_id = _seed_org_spreadsheet(isolated_api, member)
    url = (
        f"/documents/{document_id}/versions/{version_id}"
        "/sheets/sheet_0001/rows"
    )
    assert isolated_api.get(url, headers=headers).status_code == 200

    db = isolated_api.app.state.user_db
    delete_to_recycle(db, document_id, member, organization_authorized=True)

    assert isolated_api.get(url, headers=headers).status_code == 404


def test_org_spreadsheet_rows_reject_a_recycled_version(isolated_api, make_user):
    member, headers = make_user("org-member-version")
    document_id, version_id = _seed_org_spreadsheet(isolated_api, member)
    url = (
        f"/documents/{document_id}/versions/{version_id}"
        "/sheets/sheet_0001/rows"
    )
    db = isolated_api.app.state.user_db
    db.set_version_status(version_id, "recycle_pending")

    assert isolated_api.get(url, headers=headers).status_code == 404
