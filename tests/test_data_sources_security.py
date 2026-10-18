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


def test_source_list_redacts_every_secret_key_but_keeps_url_masking(
    isolated_api, make_user, monkeypatch
):
    """密钥类键整字段遮蔽（``dsn`` 亦然），非密钥键里的 URL 仍只掩凭据。"""
    _, headers = make_user("admin", is_admin=True)
    monkeypatch.setattr(settings, "data_sources_enabled", True)
    db = isolated_api.app.state.user_db
    db.create_data_source(
        "mixed",
        "database",
        '{"connection_string":"postgresql://user:pass@db/name",'
        '"dsn":"postgresql://user:pass@db/name",'
        '"db_password":"db-secret","auth_token":"Bearer tok",'
        '"feed_url":"https://user:toksecret@api.example.com/data?password=qp&x=1",'
        '"plain_url":"https://example.com/feed.xml"}',
    )

    response = isolated_api.get("/sources", headers=headers)

    assert response.status_code == 200
    config = response.json()["sources"][0]["config"]
    for key in ("connection_string", "dsn", "db_password", "auth_token"):
        assert config[key] == "[REDACTED]", key
    # 非密钥键里的 URL 仍然保留 host/path，只掩 userinfo 与敏感 query 参数。
    assert "api.example.com" in config["feed_url"]
    assert "toksecret" not in config["feed_url"]
    assert "password=***" in config["feed_url"]
    assert "x=1" in config["feed_url"]
    assert config["plain_url"] == "https://example.com/feed.xml"
    assert "user:pass" not in response.text
    assert "db-secret" not in response.text


def test_boundary_and_repository_share_one_redaction_semantics():
    """``/sources`` 边界与 ``UserDB`` 仓库层必须给出同一套脱敏语义（禁止第二份实现）。"""
    from rag.api_security import mask_source_config
    from rag.user_db import mask_data_source_config

    config = {
        "connection_string": "postgresql://user:pass@db/name",
        "url": "https://user:toksecret@host/path?token=q&x=1",
    }

    boundary, boundary_has_secret = mask_source_config(config)
    repository, repository_has_secret = mask_data_source_config(config)

    assert boundary == repository
    assert boundary_has_secret is True
    assert repository_has_secret is True
    assert repository["connection_string"] == "[REDACTED]"
    assert repository["url"] == "https://user:***@host/path?token=***&x=1"
