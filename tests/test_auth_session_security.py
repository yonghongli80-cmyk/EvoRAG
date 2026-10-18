from config import settings


def test_self_registration_is_closed_by_default(isolated_api, monkeypatch):
    monkeypatch.setattr(settings, "self_registration_enabled", False, raising=False)

    response = isolated_api.post(
        "/register",
        json={"username": "outsider", "password": "Password-123!"},
    )

    assert response.status_code == 404


def test_login_sets_http_only_strict_session_cookie(isolated_api, make_user):
    make_user("cookie-user")

    response = isolated_api.post(
        "/login",
        json={"username": "cookie-user", "password": "Password-123!"},
    )

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "rag_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert isolated_api.get("/me").status_code == 200


def test_login_rate_limits_repeated_failures(isolated_api, make_user):
    make_user("rate-limited-user")

    for _ in range(5):
        response = isolated_api.post(
            "/login",
            json={"username": "rate-limited-user", "password": "wrong-password"},
        )
        assert response.status_code == 401

    blocked = isolated_api.post(
        "/login",
        json={"username": "rate-limited-user", "password": "Password-123!"},
    )
    assert blocked.status_code == 429


def test_logout_clears_browser_session(isolated_api, make_user):
    make_user("logout-user")
    isolated_api.post(
        "/login",
        json={"username": "logout-user", "password": "Password-123!"},
    )
    assert isolated_api.get("/me").status_code == 200

    response = isolated_api.post("/logout")

    assert response.status_code == 204
    assert isolated_api.get("/me").status_code == 401


def test_login_accepts_an_unambiguous_employee_name(isolated_api, make_user):
    user, _ = make_user("demo-name-login")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P-NAME',?,'林知远','专员')",
            (user["id"],),
        )

    response = isolated_api.post(
        "/login",
        json={"username": "林知远", "password": "Password-123!"},
    )

    assert response.status_code == 200
    assert response.json()["username"] == "demo-name-login"


def test_cookie_authenticated_write_rejects_cross_origin_request(
    isolated_api, make_user
):
    make_user("csrf-user")
    isolated_api.post(
        "/login",
        json={"username": "csrf-user", "password": "Password-123!"},
    )

    response = isolated_api.post(
        "/logout",
        headers={"Origin": "https://attacker.invalid"},
    )

    assert response.status_code == 403
    assert isolated_api.get("/me").status_code == 200


def test_cookie_authenticated_write_accepts_https_origin_with_forwarded_proto(
    isolated_api, make_user
):
    make_user("forwarded-user")
    isolated_api.post(
        "/login",
        json={"username": "forwarded-user", "password": "Password-123!"},
    )

    response = isolated_api.post(
        "/logout",
        headers={
            "Origin": "https://testserver",
            "X-Forwarded-Proto": "https",
        },
    )

    assert response.status_code == 204
    assert isolated_api.get("/me").status_code == 401
