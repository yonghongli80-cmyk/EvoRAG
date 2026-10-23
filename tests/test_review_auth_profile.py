import pytest


@pytest.mark.parametrize("is_admin", [False, True])
@pytest.mark.parametrize("has_profile", [False, True])
def test_me_returns_admin_flag_with_or_without_org_profile(
    isolated_api, make_user, is_admin, has_profile
):
    import rag.api as api
    from rag.org_auth.repository import OrgRepository

    db = api.app.state.user_db
    OrgRepository(db).install_schema()
    username = f"me-{is_admin}-{has_profile}"
    user, headers = make_user(username, is_admin=is_admin)
    if has_profile:
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
                "VALUES(?,?,?,?)",
                (f"P-{user['id']}", user["id"], username, "工程师"),
            )

    response = isolated_api.get("/me", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["is_admin"] is is_admin
    assert ("profile" in body) is has_profile
