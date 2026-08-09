from config import Settings, settings


def test_security_settings_fail_closed(monkeypatch):
    monkeypatch.delenv("RAG_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("RAG_DATA_SOURCES_ENABLED", raising=False)

    configured = Settings(_env_file=None)

    assert configured.auth_enabled is True
    assert getattr(configured, "data_sources_enabled", True) is False


def test_sources_are_hidden_when_feature_is_disabled(isolated_api, make_user, monkeypatch):
    monkeypatch.setattr(settings, "data_sources_enabled", False)
    _, headers = make_user("admin", is_admin=True)

    response = isolated_api.get("/sources", headers=headers)

    assert response.status_code == 404


def test_enabled_sources_reject_non_admin(isolated_api, make_user, monkeypatch):
    _, headers = make_user("alice")
    monkeypatch.setattr(settings, "data_sources_enabled", True)

    response = isolated_api.get("/sources", headers=headers)

    assert response.status_code == 403


def test_enabled_source_list_redacts_credentials(isolated_api, make_user, monkeypatch):
    _, headers = make_user("admin", is_admin=True)
    monkeypatch.setattr(settings, "data_sources_enabled", True)
    db = isolated_api.app.state.user_db
    db.create_data_source(
        "internal-api",
        "api",
        '{"url":"https://example.invalid","headers":{"Authorization":"Bearer secret"},'
        '"password":"db-secret","connection_string":"postgresql://user:pass@db/name"}',
    )

    response = isolated_api.get("/sources", headers=headers)

    assert response.status_code == 200
    config = response.json()["sources"][0]["config"]
    assert config["headers"]["Authorization"] == "[REDACTED]"
    assert config["password"] == "[REDACTED]"
    assert config["connection_string"] == "[REDACTED]"
    assert "Bearer secret" not in response.text
    assert "db-secret" not in response.text
    assert "user:pass" not in response.text
