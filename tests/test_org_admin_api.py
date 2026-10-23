def test_account_directory_and_admin_preview(isolated_api, make_user):
    import rag.api as api
    admin, headers = make_user("admin", is_admin=True)
    employee, _ = make_user("fixed001")
    db = api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('ROOT','星河集团','company')")
        conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'林知远','专员')", (employee["id"],))
        conn.execute("INSERT INTO org_memberships VALUES('P1','ROOT',1)")
    assert isolated_api.get("/login/accounts?q=林").status_code == 401
    directory = isolated_api.get("/login/accounts?q=林", headers=headers).json()
    assert directory == [{"username": "fixed001", "person_code": "P1", "display_name": "林知远", "job_title": "专员", "department": "星河集团"}]
    preview = isolated_api.get(f"/organization/preview/{employee['id']}", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["user_id"] == employee["id"]
    assert "token" not in preview.json()


def test_employee_cannot_mutate_organization(isolated_api, make_user):
    employee, headers = make_user("employee")
    response = isolated_api.patch("/organization/people/P1", json={"active": False}, headers=headers)
    assert response.status_code == 404


def test_org_kb_detail_uses_durable_metadata_without_vector_collection(isolated_api, make_user):
    import rag.api as api
    from rag.auth import create_token
    employee, headers = make_user("member")
    db = api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','协作组','team')")
        conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'甲','专员')", (employee["id"],))
        conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb','协作知识库')")
        conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('kb','TEAM','organization')")
    headers = {"Authorization": f"Bearer {create_token({'user_id': employee['id'], 'username': 'member', 'security_version': 1})}"}
    response = isolated_api.get('/knowledge-bases/kb', headers=headers)
    assert response.status_code == 200
    assert response.json()["effective_role"] == "read"
    assert response.json()["documents"] == []


def test_admin_relationship_changes_invalidate_old_token(isolated_api, make_user):
    from rag.api_security import resolve_principal
    from rag.auth import create_token

    admin, admin_headers = make_user("admin", is_admin=True)
    employee, _ = make_user("member")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('ROOT','集团','company')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P1',?,'甲','专员')", (employee["id"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P1','ROOT',1)")
    token = create_token({
        "user_id": employee["id"], "username": "member", "security_version": 1,
    })
    assert resolve_principal(db, f"Bearer {token}", None) is not None

    response = isolated_api.patch(
        "/organization/people/P1",
        headers=admin_headers,
        json={"manager_org_codes": ["ROOT"], "sensitivities": ["finance"]},
    )

    assert response.status_code == 200
    assert resolve_principal(db, f"Bearer {token}", None) is None


def test_grant_rejects_unknown_subject_and_knowledge_base(isolated_api, make_user):
    _admin, headers = make_user("admin", is_admin=True)
    invalid = {
        "kb_id": "missing",
        "subject_type": "org",
        "subject_id": "missing",
        "role": "read",
        "effect": "allow",
    }
    assert isolated_api.post("/organization/grants", headers=headers, json=invalid).status_code == 404


def test_admin_can_revoke_grant_and_invalidate_subject_token(isolated_api, make_user):
    from rag.api_security import resolve_principal
    from rag.auth import create_token

    _admin, headers = make_user("admin", is_admin=True)
    employee, _ = make_user("member")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('ROOT','集团','company')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P1',?,'甲','专员')", (employee["id"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P1','ROOT',1)")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb','知识库')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES('kb','ROOT','organization')"
        )
    created = isolated_api.post("/organization/grants", headers=headers, json={
        "kb_id": "kb", "subject_type": "user", "subject_id": str(employee["id"]),
        "role": "read", "effect": "allow",
    })
    assert created.status_code == 200
    with db.locked_read() as conn:
        version = conn.execute(
            "SELECT security_version FROM org_people WHERE user_id=?", (employee["id"],)
        ).fetchone()[0]
    token = create_token({
        "user_id": employee["id"], "username": "member", "security_version": version,
    })
    assert resolve_principal(db, f"Bearer {token}", None) is not None

    revoked = isolated_api.delete(
        f"/organization/grants/{created.json()['id']}", headers=headers
    )

    assert revoked.status_code == 200
    assert resolve_principal(db, f"Bearer {token}", None) is None


def test_admin_can_assign_an_unassigned_kb_to_an_org(isolated_api, make_user):
    admin, admin_headers = make_user("admin", is_admin=True)
    member, member_headers = make_user("member")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','协作组','team')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P1',?,'甲','专员')",
            (member["id"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb','待归属库')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES('kb',NULL,'unassigned')"
        )

    assert isolated_api.get("/knowledge-bases/kb", headers=member_headers).status_code == 404

    assigned = isolated_api.put(
        "/organization/knowledge-bases/kb/policy",
        headers=admin_headers,
        json={"org_code": "TEAM", "kb_type": "organization"},
    )

    assert assigned.status_code == 200
    assert assigned.json()["org_code"] == "TEAM"
    assert isolated_api.get("/knowledge-bases/kb", headers=member_headers).status_code == 200
    with db.locked_read() as conn:
        row = conn.execute(
            "SELECT org_code,kb_type FROM kb_org_policies WHERE kb_id='kb'"
        ).fetchone()
    assert (row["org_code"], row["kb_type"]) == ("TEAM", "organization")

    denied = isolated_api.put(
        "/organization/knowledge-bases/kb/policy",
        headers=member_headers,
        json={"org_code": "TEAM", "kb_type": "organization"},
    )
    assert denied.status_code == 404

    unknown = isolated_api.put(
        "/organization/knowledge-bases/kb/policy",
        headers=admin_headers,
        json={"org_code": "MISSING", "kb_type": "organization"},
    )
    assert unknown.status_code == 404
    assert admin["id"] != member["id"]
