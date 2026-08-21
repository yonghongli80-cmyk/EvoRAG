from rag.org_auth.audit import OrgAuditService
from rag.user_db import UserDB


def test_duplicate_kb_metadata_does_not_poison_following_transaction(tmp_path):
    db = UserDB(str(tmp_path / "duplicate.db"))
    try:
        db.create_kb_metadata("same-kb", "First")
        db.create_kb_metadata("same-kb", "Duplicate")

        event_id = OrgAuditService(db).record(
            None,
            "integrity_probe",
            "knowledge_base",
            "same-kb",
            "success",
            "transaction_available",
        )

        assert event_id > 0
        assert db.get_kb_metadata("same-kb")["name"] == "First"
    finally:
        db.close()


def test_partial_primary_department_update_preserves_secondary_memberships(
    isolated_api, make_user
):
    admin, headers = make_user("membership-admin", is_admin=True)
    employee, _ = make_user("membership-person")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.executemany(
            "INSERT INTO org_units(code,name,kind) VALUES(?,?,?)",
            [("A", "甲部门", "department"), ("B", "乙部门", "department"), ("C", "丙部门", "department")],
        )
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P-MEMBER',?,'成员','专员')",
            (employee["id"],),
        )
        conn.executemany(
            "INSERT INTO org_memberships(person_code,org_code,is_primary) VALUES(?,?,?)",
            [("P-MEMBER", "A", 1), ("P-MEMBER", "B", 0)],
        )

    response = isolated_api.patch(
        "/organization/people/P-MEMBER",
        headers=headers,
        json={"primary_org_code": "C"},
    )

    assert response.status_code == 200
    with db.locked_read() as conn:
        memberships = [tuple(row) for row in conn.execute(
            "SELECT org_code,is_primary FROM org_memberships "
            "WHERE person_code='P-MEMBER' ORDER BY org_code"
        )]
    assert memberships == [("B", 0), ("C", 1)]
