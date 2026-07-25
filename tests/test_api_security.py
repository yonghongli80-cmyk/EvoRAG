from config import settings


def test_bearer_token_resolves_full_database_user(isolated_api, make_user):
    user, headers = make_user("alice")

    response = isolated_api.get("/me", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"id": user["id"], "username": "alice"}


def test_malformed_bearer_prefix_is_rejected_with_401(isolated_api, make_user):
    user, headers = make_user("alice")
    malformed_headers = {"Authorization": headers["Authorization"].replace("Bearer", "bearer")}

    response = isolated_api.get("/me", headers=malformed_headers)

    assert response.status_code == 401


def test_configured_api_key_resolves_a_real_user(isolated_api, make_user, monkeypatch):
    user, _ = make_user("service-admin", is_admin=True)
    monkeypatch.setattr(settings, "auth_keys", '{"service-admin":"key-123"}')

    response = isolated_api.get("/me", headers={"X-API-Key": "key-123"})

    assert response.status_code == 200
    assert response.json() == {"id": user["id"], "username": "service-admin"}


def test_api_uses_the_isolated_database(isolated_api):
    assert isolated_api.app.state.user_db.db_path != settings.users_db_path
