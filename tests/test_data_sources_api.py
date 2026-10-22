"""TDD tests for data source management API endpoints."""

import sys
from unittest.mock import ANY, patch

import pytest
from fastapi.testclient import TestClient

from config import settings
from rag.api import app, user_db
from rag.auth import create_token


@pytest.fixture(autouse=True)
def _clean_data_sources(sandbox_api_storage, monkeypatch):
    """Delete all data_sources rows before and after each test to avoid cross-test pollution."""
    monkeypatch.setattr(sys.modules[__name__], "user_db", sandbox_api_storage)
    with user_db._lock:
        user_db._conn.execute("DELETE FROM data_sources")
        user_db._conn.commit()
    yield
    with user_db._lock:
        user_db._conn.execute("DELETE FROM data_sources")
        user_db._conn.commit()


client = TestClient(app)


@pytest.fixture(autouse=True)
def _enable_sources_for_admin_tests(monkeypatch):
    """Exercise source management only with its explicit feature flag and admin."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "data_sources_enabled", True)
    try:
        user_id = user_db.create_user("_data_source_test_admin", "Password-123!")
    except ValueError:
        user_id = user_db.get_user_by_username("_data_source_test_admin")["id"]
    user_db.set_user_admin(user_id, True)
    previous_headers = dict(client.headers)
    client.headers.update({
        "Authorization": f"Bearer {create_token({'user_id': user_id, 'username': '_data_source_test_admin'})}"
    })
    yield
    client.headers.clear()
    client.headers.update(previous_headers)


# ── POST /sources ────────────────────────────────────────────────────────────


class TestCreateSource:
    def test_create_rss_source(self):
        response = client.post(
            "/sources",
            json={
                "name": "My RSS Feed",
                "type": "rss",
                "config": {"url": "https://example.com/feed.xml"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "My RSS Feed"
        assert data["type"] == "rss"
        assert "id" in data
        assert data["status"] == "inactive"

    def test_create_db_source(self):
        response = client.post(
            "/sources",
            json={
                "name": "My Database",
                "type": "database",
                "config": {
                    "connection_string": "sqlite:///test.db",
                    "query": "SELECT * FROM articles",
                },
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "database"
        assert "id" in data

    def test_create_api_source(self):
        response = client.post(
            "/sources",
            json={
                "name": "My API",
                "type": "api",
                "config": {
                    "url": "https://api.example.com/data",
                    "headers": {"Authorization": "Bearer token"},
                },
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "api"

    def test_create_source_missing_name(self):
        response = client.post(
            "/sources",
            json={"type": "rss", "config": {"url": "https://example.com/feed.xml"}},
        )
        assert response.status_code == 422

    def test_create_source_missing_type(self):
        response = client.post(
            "/sources",
            json={"name": "No Type", "config": {}},
        )
        assert response.status_code == 422

    def test_create_source_invalid_type(self):
        """FastAPI/Pydantic rejects values that don't match the pattern."""
        response = client.post(
            "/sources",
            json={"name": "Bad", "type": "invalid_type", "config": {}},
        )
        assert response.status_code == 422


# ── GET /sources ─────────────────────────────────────────────────────────────


class TestListSources:
    def test_list_empty(self):
        response = client.get("/sources")
        assert response.status_code == 200
        data = response.json()
        assert data["sources"] == []
        assert data["count"] == 0

    def test_list_after_create(self):
        create_resp = client.post(
            "/sources",
            json={
                "name": "Test Feed",
                "type": "rss",
                "config": {"url": "https://example.com/feed.xml"},
            },
        )
        assert create_resp.status_code == 200

        response = client.get("/sources")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1
        names = [s["name"] for s in data["sources"]]
        assert "Test Feed" in names

    def test_list_after_create_multiple(self):
        client.post(
            "/sources",
            json={"name": "RSS 1", "type": "rss", "config": {"url": "https://a.com/f"}},
        )
        client.post(
            "/sources",
            json={"name": "API 1", "type": "api", "config": {"url": "https://b.com"}},
        )
        response = client.get("/sources")
        assert response.status_code == 200
        assert response.json()["count"] >= 2


# ── DELETE /sources/{id} ────────────────────────────────────────────────────


class TestDeleteSource:
    def test_delete_existing(self):
        create_resp = client.post(
            "/sources",
            json={"name": "To Delete", "type": "rss", "config": {"url": "https://x.com/f"}},
        )
        source_id = create_resp.json()["id"]

        response = client.delete(f"/sources/{source_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"

        # Verify gone
        list_resp = client.get("/sources")
        ids = [s["id"] for s in list_resp.json()["sources"]]
        assert source_id not in ids

    def test_delete_nonexistent(self):
        response = client.delete("/sources/99999")
        assert response.status_code == 404


# ── POST /sources/{id}/sync ────────────────────────────────────────────────


class TestSyncSource:
    def test_sync_rss_source(self):
        create_resp = client.post(
            "/sources",
            json={"name": "Sync RSS", "type": "rss", "config": {"url": "https://example.com/feed"}},
        )
        source_id = create_resp.json()["id"]

        with patch("rag.routers.sources._sync_source") as mock_sync:
            mock_sync.return_value = {"synced": 5, "errors": []}
            response = client.post(f"/sources/{source_id}/sync")
            assert response.status_code == 200
            data = response.json()
            assert data["synced"] == 5
            assert data["errors"] == []
            # 同步执行体由路由以当前 UserDB 与显式主体注入（B-03）。
            mock_sync.assert_called_once_with(source_id, db=user_db, user_id=ANY)

    def test_sync_nonexistent(self):
        response = client.post("/sources/99999/sync")
        assert response.status_code == 404

    def test_sync_updates_last_synced(self):
        """When _sync_source runs, last_synced_at should be updated in DB."""
        create_resp = client.post(
            "/sources",
            json={"name": "Sync Check", "type": "rss", "config": {"url": "https://x.com/f"}},
        )
        source_id = create_resp.json()["id"]

        # Patch the module-level _sync_source so it does the DB updates
        # without making real HTTP requests. The route now injects the active
        # UserDB as the `db` keyword argument.
        def _fake_sync(sid, db=None, user_id=None):
            target_db = db if db is not None else user_db
            target_db.update_data_source_synced(sid)
            target_db.update_data_source_status(sid, "active")
            return {"synced": 0, "errors": []}

        with patch("rag.routers.sources._sync_source", side_effect=_fake_sync):
            client.post(f"/sources/{source_id}/sync")

        # Verify last_synced_at was updated
        list_resp = client.get("/sources")
        source = next(s for s in list_resp.json()["sources"] if s["id"] == source_id)
        assert source["last_synced_at"] is not None
