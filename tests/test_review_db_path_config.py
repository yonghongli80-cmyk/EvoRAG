import sqlite3

from fastapi.testclient import TestClient

import rag.vector_store as vector_store
from config import settings
from rag.api import app
from rag.user_db import UserDB


def _qdrant_ready(monkeypatch):
    client = type("Client", (), {"get_collections": lambda self: []})()
    monkeypatch.setattr(vector_store, "_get_client", lambda: client)
    monkeypatch.setattr(vector_store, "client_mode", lambda: "local")


def _create_analysis_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("SELECT 1")


def test_user_db_defaults_to_configured_users_database(tmp_path, monkeypatch):
    target = tmp_path / "configured-users.db"
    monkeypatch.setattr(settings, "users_db_path", str(target))

    db = UserDB()
    try:
        assert db.db_path == str(target)
        user_id = db.create_user("default-path-user", "Password-123!")
        assert db.get_user_by_id(user_id)["username"] == "default-path-user"
    finally:
        db.close()


def test_health_and_ready_use_configured_users_database(tmp_path, monkeypatch):
    users_path = tmp_path / "users.db"
    analysis_path = tmp_path / "analysis.db"
    monkeypatch.setattr(settings, "users_db_path", str(users_path))
    monkeypatch.setattr(settings, "analysis_db_path", str(analysis_path))
    db = UserDB(str(users_path))
    db.close()
    _qdrant_ready(monkeypatch)
    monkeypatch.setattr(settings, "redis_url", "")

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {
        "status": "healthy",
        "components": {"qdrant": "ok", "sqlite": "ok"},
    }
    assert ready.status_code == 200
    assert ready.json()["components"]["sqlite"] == {"status": "ok"}
    assert not analysis_path.exists()


def test_healthy_analysis_database_cannot_mask_missing_users_database(
    tmp_path, monkeypatch
):
    users_path = tmp_path / "missing-users.db"
    analysis_path = tmp_path / "analysis.db"
    _create_analysis_db(analysis_path)
    monkeypatch.setattr(settings, "users_db_path", str(users_path))
    monkeypatch.setattr(settings, "analysis_db_path", str(analysis_path))
    monkeypatch.setattr(settings, "redis_url", "")
    _qdrant_ready(monkeypatch)

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 503
    assert health.json() == {
        "status": "degraded",
        "components": {"qdrant": "ok", "sqlite": "error"},
    }
    assert ready.status_code == 503
    assert ready.json()["components"]["sqlite"] == {
        "status": "error",
        "detail": "sqlite_unavailable",
    }
    assert not users_path.exists()


def test_healthy_analysis_database_cannot_mask_corrupt_users_database(
    tmp_path, monkeypatch
):
    users_path = tmp_path / "corrupt-users.db"
    analysis_path = tmp_path / "analysis.db"
    users_path.write_bytes(b"not a sqlite database")
    _create_analysis_db(analysis_path)
    monkeypatch.setattr(settings, "users_db_path", str(users_path))
    monkeypatch.setattr(settings, "analysis_db_path", str(analysis_path))
    monkeypatch.setattr(settings, "redis_url", "")
    _qdrant_ready(monkeypatch)

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 503
    assert health.json()["components"]["sqlite"] == "error"
    assert ready.status_code == 503
    assert ready.json()["components"]["sqlite"] == {
        "status": "error",
        "detail": "sqlite_unavailable",
    }
    assert "not a sqlite database" not in health.text + ready.text
    assert users_path.read_bytes() == b"not a sqlite database"
