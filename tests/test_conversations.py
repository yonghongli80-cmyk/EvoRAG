"""Tests for conversation management API endpoints."""

import pytest
from fastapi.testclient import TestClient

from config import settings
from rag.user_db import UserDB


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Yield a TestClient with a temporary UserDB patched in."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    db_path = str(tmp_path / "test_users.db")
    import rag.api as api_mod

    original_db = api_mod.user_db
    api_mod.user_db = UserDB(db_path)
    with TestClient(api_mod.app) as c:
        yield c
    api_mod.user_db = original_db


def _register_and_login(client, username="conv_user", password="Conv_pass_123"):
    """Helper: register a user and return the JWT token."""
    client.post("/register", json={"username": username, "password": password})
    resp = client.post("/login", json={"username": username, "password": password})
    return resp.json()["token"]


def test_create_conversation(client):
    token = _register_and_login(client, "conv_create", "Pass12345")
    resp = client.post(
        "/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["title"] == "新对话"


def test_list_conversations(client):
    token = _register_and_login(client, "conv_list", "Pass12345")
    # Create two conversations
    client.post("/conversations", headers={"Authorization": f"Bearer {token}"})
    client.post("/conversations", headers={"Authorization": f"Bearer {token}"})
    resp = client.get(
        "/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2


def test_delete_conversation(client):
    token = _register_and_login(client, "conv_delete", "Pass12345")
    create_resp = client.post(
        "/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    cid = create_resp.json()["id"]
    resp = client.delete(
        f"/conversations/{cid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


def test_unauthorized(client):
    resp = client.get("/conversations")
    assert resp.status_code in (401, 403, 422)
