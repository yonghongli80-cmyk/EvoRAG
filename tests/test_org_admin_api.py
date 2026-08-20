def test_account_directory_and_admin_preview(isolated_api, make_user):
    import rag.api as api
    admin, headers = make_user("admin", is_admin=True)
    employee, _ = make_user("fixed001")
    db = api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('ROOT','星河集团','company')")
        conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'林知远','专员')", (employee["id"],))
        conn.execute("INSERT INTO org_memberships VALUES('P1','ROOT',1)")
    directory = isolated_api.get("/login/accounts?q=林").json()
    assert directory == [{"username": "fixed001", "person_code": "P1", "display_name": "林知远", "job_title": "专员", "department": "星河集团"}]
    preview = isolated_api.get(f"/organization/preview/{employee['id']}", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["user_id"] == employee["id"]
    assert "token" not in preview.json()


def test_employee_cannot_mutate_organization(isolated_api, make_user):
    employee, headers = make_user("employee")
    response = isolated_api.patch("/organization/people/P1", json={"active": False}, headers=headers)
    assert response.status_code == 404
