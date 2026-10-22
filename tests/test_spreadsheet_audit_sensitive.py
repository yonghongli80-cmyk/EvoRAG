"""D-09: sensitive audit values are redacted per domain grant, not flagged 0."""

from __future__ import annotations

import hashlib

import pytest
from openpyxl import Workbook

from rag.spreadsheets.audit import (
    document_sensitive_domain,
    get_spreadsheet_audit_detail,
    viewer_holds_sensitivity_grant,
)
from rag.spreadsheets.edit_models import (
    CellPatch,
    RequestMeta,
    SpreadsheetEditRequest,
)
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset
from rag.user_db import UserDB


def _reserve_session(db: UserDB, document_id: int, version_id: int, actor_id: int, *, sensitive: int):
    session_id, _ = db.reserve_spreadsheet_edit(
        document_id=document_id,
        base_version_id=version_id,
        actor_user_id=actor_id,
        original_filename="payroll.xlsx",
        staged_path="v2.xlsx",
        content_hash="hash-2",
        operation="edit",
        reason="raise correction",
        restored_from_version_id=None,
        risk_flags=(),
        changes=(
            {
                "sheet_key": "sheet_0001",
                "sheet_name_snapshot": "Payroll",
                "cell_address": "B2",
                "old_value_type": "number",
                "old_value_json": "8800",
                "new_value_type": "number",
                "new_value_json": "9200",
                "sensitive": sensitive,
            },
        ),
    )
    return session_id


@pytest.fixture()
def audit_context(tmp_path):
    db = UserDB(str(tmp_path / "audit.db"))
    owner_id = db.create_user("audit-owner", "Password-123!")
    admin_id = db.create_user("audit-admin", "Password-123!")
    db.set_user_admin(admin_id, True)
    cleared_admin_id = db.create_user("audit-cleared-admin", "Password-123!")
    db.set_user_admin(cleared_admin_id, True)
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_units(code,parent_code,name,kind) "
            "VALUES('TEAM',NULL,'团队','team')"
        )
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P-CLEARED',?,'已授权审计','审计')",
            (cleared_admin_id,),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P-CLEARED','TEAM',1)")
        conn.execute(
            "INSERT INTO org_sensitive_qualifications VALUES('P-CLEARED','finance')"
        )
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type,sensitivity) "
            "VALUES('kb-secret','TEAM','sensitive','finance')"
        )
    document_id = db.create_document_permission("payroll.xlsx", "kb-secret", owner_id)
    version_id = db.create_document_version(
        document_id, "payroll.xlsx", "v1.xlsx", "hash-1", owner_id
    )
    db.activate_document_version(document_id, version_id, "kb-secret", "payroll.xlsx")
    yield {
        "db": db,
        "owner": db.get_user_by_id(owner_id),
        "admin": db.get_user_by_id(admin_id),
        "cleared_admin": db.get_user_by_id(cleared_admin_id),
        "document_id": document_id,
        "version_id": version_id,
        "tmp_path": tmp_path,
    }
    db.close()


def test_domain_and_grant_helpers_follow_the_org_policy(audit_context):
    db = audit_context["db"]

    assert document_sensitive_domain(db, "kb-secret") == "finance"
    assert document_sensitive_domain(db, "rag_docs") is None
    assert viewer_holds_sensitivity_grant(db, audit_context["cleared_admin"], "finance")
    assert not viewer_holds_sensitivity_grant(db, audit_context["admin"], "finance")
    assert not viewer_holds_sensitivity_grant(db, audit_context["admin"], None)


def test_unqualified_admin_cannot_read_sensitive_old_or_new_values(audit_context):
    db = audit_context["db"]
    session_id = _reserve_session(
        db,
        audit_context["document_id"],
        audit_context["version_id"],
        audit_context["owner"]["id"],
        sensitive=1,
    )

    detail = get_spreadsheet_audit_detail(db, audit_context["admin"], session_id)

    assert detail is not None
    change = detail["sheets"][0]["changes"][0]
    assert change["redacted"] is True
    assert change["redaction_reason"] == "sensitivity_required"
    assert "old_value" not in change
    assert "new_value" not in change
    assert "8800" not in repr(detail)
    assert "9200" not in repr(detail)


def test_owner_without_a_grant_is_redacted_too(audit_context):
    db = audit_context["db"]
    session_id = _reserve_session(
        db,
        audit_context["document_id"],
        audit_context["version_id"],
        audit_context["owner"]["id"],
        sensitive=1,
    )

    detail = get_spreadsheet_audit_detail(db, audit_context["owner"], session_id)

    change = detail["sheets"][0]["changes"][0]
    assert change["redacted"] is True
    assert "old_value" not in change


def test_admin_holding_the_domain_grant_sees_the_values(audit_context):
    db = audit_context["db"]
    session_id = _reserve_session(
        db,
        audit_context["document_id"],
        audit_context["version_id"],
        audit_context["owner"]["id"],
        sensitive=1,
    )

    detail = get_spreadsheet_audit_detail(
        db, audit_context["cleared_admin"], session_id
    )

    change = detail["sheets"][0]["changes"][0]
    assert "redacted" not in change
    assert change["old_value"] == 8800
    assert change["new_value"] == 9200


def test_rows_written_before_the_flag_are_derived_from_the_domain(audit_context):
    db = audit_context["db"]
    # ``sensitive=0`` is what the historical code always stored; the read path
    # must still protect it because the document lives in a sensitive domain.
    session_id = _reserve_session(
        db,
        audit_context["document_id"],
        audit_context["version_id"],
        audit_context["owner"]["id"],
        sensitive=0,
    )

    detail = get_spreadsheet_audit_detail(db, audit_context["admin"], session_id)

    change = detail["sheets"][0]["changes"][0]
    assert change["redacted"] is True
    assert "old_value" not in change


def test_missing_domain_fails_closed(audit_context):
    db = audit_context["db"]
    session_id = _reserve_session(
        db,
        audit_context["document_id"],
        audit_context["version_id"],
        audit_context["owner"]["id"],
        sensitive=1,
    )
    with db.locked_transaction() as conn:
        conn.execute("DELETE FROM kb_org_policies WHERE kb_id='kb-secret'")

    detail = get_spreadsheet_audit_detail(db, audit_context["admin"], session_id)

    change = detail["sheets"][0]["changes"][0]
    assert change["redacted"] is True
    assert change["redaction_reason"] == "sensitivity_domain_unknown"
    assert "old_value" not in change


def test_legacy_kb_values_remain_visible(audit_context):
    db = audit_context["db"]
    owner = audit_context["owner"]
    document_id = db.create_document_permission("prices.xlsx", "rag_docs", owner["id"])
    version_id = db.create_document_version(
        document_id, "prices.xlsx", "v1.xlsx", "hash-1", owner["id"]
    )
    db.activate_document_version(document_id, version_id, "rag_docs", "prices.xlsx")
    session_id = _reserve_session(db, document_id, version_id, owner["id"], sensitive=0)

    detail = get_spreadsheet_audit_detail(db, owner, session_id)

    change = detail["sheets"][0]["changes"][0]
    assert "redacted" not in change
    assert change["old_value"] == 8800


def _seed_editable_workbook(db: UserDB, tmp_path, kb_id: str, owner_id: int):
    from rag.document_version_store import final_version_path

    filename = f"editable-{kb_id}.xlsx"
    document_id = db.create_document_permission(filename, kb_id, owner_id)
    versions_root = tmp_path / "document_versions"
    structured_root = tmp_path / "structured_datasets"
    source = final_version_path(versions_root, document_id, 1, filename)
    source.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.title = "Payroll"
    book.active.append(["employee", "salary"])
    book.active.append(["张三", 8800])
    book.save(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    version_id = db.create_document_version(
        document_id, filename, str(source), source_hash, owner_id
    )
    dataset_id = db.create_structured_dataset(
        document_id, version_id, "xlsx", "row_groups"
    )
    dataset_path = resolve_dataset_path(structured_root, document_id, version_id)
    write_dataset(dataset_path, parse_spreadsheet(source, "xlsx", None))
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id=kb_id,
        filename=filename,
    )
    return document_id, version_id, versions_root, structured_root


def test_save_records_the_sensitive_flag_for_a_sensitive_domain(audit_context):
    from rag.spreadsheets.edit_service import SpreadsheetEditService

    db = audit_context["db"]
    admin = audit_context["admin"]
    document_id, version_id, versions_root, structured_root = _seed_editable_workbook(
        db, audit_context["tmp_path"], "kb-secret", admin["id"]
    )
    service = SpreadsheetEditService(
        db, versions_root, structured_root, enqueue=lambda *args: 0
    )

    result = service.save(
        document_id,
        admin,
        SpreadsheetEditRequest(
            base_version_id=version_id,
            changes=(
                CellPatch(
                    sheet_key="sheet_0001",
                    cell="B2",
                    value_type="number",
                    value=9200,
                ),
            ),
            reason="raise",
        ),
        RequestMeta(ip_address="127.0.0.1", user_agent="pytest"),
    )

    stored = db._conn.execute(
        "SELECT sensitive FROM spreadsheet_cell_changes WHERE session_id = ?",
        (result.session_id,),
    ).fetchone()
    assert stored["sensitive"] == 1
    detail = get_spreadsheet_audit_detail(db, admin, result.session_id)
    assert detail["sheets"][0]["changes"][0]["redacted"] is True


def test_save_keeps_the_flag_clear_for_a_legacy_kb(tmp_path):
    from rag.spreadsheets.edit_service import SpreadsheetEditService

    db = UserDB(str(tmp_path / "legacy.db"))
    try:
        owner_id = db.create_user("legacy-owner", "Password-123!")
        owner = db.get_user_by_id(owner_id)
        document_id, version_id, versions_root, structured_root = _seed_editable_workbook(
            db, tmp_path, "rag_docs", owner_id
        )
        service = SpreadsheetEditService(
            db, versions_root, structured_root, enqueue=lambda *args: 0
        )

        result = service.save(
            document_id,
            owner,
            SpreadsheetEditRequest(
                base_version_id=version_id,
                changes=(
                    CellPatch(
                        sheet_key="sheet_0001",
                        cell="B2",
                        value_type="number",
                        value=9200,
                    ),
                ),
                reason="raise",
            ),
            RequestMeta(),
        )

        stored = db._conn.execute(
            "SELECT sensitive FROM spreadsheet_cell_changes WHERE session_id = ?",
            (result.session_id,),
        ).fetchone()
        assert stored["sensitive"] == 0
    finally:
        db.close()
