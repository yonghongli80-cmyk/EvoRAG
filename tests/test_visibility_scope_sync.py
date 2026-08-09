"""Regression tests for scope-backed document visibility."""

from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

import rag.api as api_module
from config import settings
from rag.auth import create_token
from rag.models import Chunk
from rag.permissions import filter_chunks_by_permission
from rag.user_db import UserDB


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    """Isolate document files and permission records for visibility checks."""
    db = UserDB(str(tmp_path / "users.db"))
    monkeypatch.setattr(api_module, "user_db", db)
    monkeypatch.setattr(api_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "auth_enabled", True)

    with ExitStack():
        yield TestClient(api_module.app), db, tmp_path

    db.close()


def _headers(user_id: int, username: str) -> dict[str, str]:
    token = create_token({"user_id": user_id, "username": username})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("initial_public", "other_can_access"),
    [(True, False), (False, True)],
    ids=("public_to_private", "private_to_public"),
)
def test_visibility_toggle_updates_scope_for_download_and_retrieval(
    api_env, initial_public, other_can_access
):
    """A visibility toggle changes non-owner download and retrieval access."""
    client, db, data_dir = api_env
    owner_id = db.create_user("owner", "Password1")
    viewer_id = db.create_user("viewer", "Password2")
    doc_id = db.create_document_permission(
        "visibility.txt", "rag_docs", owner_id, is_public=initial_public
    )
    (data_dir / "visibility.txt").write_text("visibility content")

    response = client.put(
        "/files/visibility.txt/visibility", headers=_headers(owner_id, "owner")
    )
    assert response.status_code == 200, response.text

    perm = db.get_document_permission_by_id(doc_id)
    assert perm["scope"] == ("public" if other_can_access else "private")
    assert perm["is_public"] is other_can_access

    viewer = db.get_user_by_id(viewer_id)
    chunks = [Chunk(text="visibility content", doc_name="visibility.txt", chunk_index=0)]
    visible_chunks = filter_chunks_by_permission(db, "rag_docs", chunks, viewer)
    assert bool(visible_chunks) is other_can_access

    download = client.get(
        "/files/visibility.txt/download", headers=_headers(viewer_id, "viewer")
    )
    assert download.status_code == (200 if other_can_access else 403), download.text


def test_retrieval_drops_document_without_permission_record(api_env):
    """A missing permission record never exposes a chunk to a non-admin user."""
    _, db, _ = api_env
    viewer_id = db.create_user("viewer", "Password2")
    viewer = db.get_user_by_id(viewer_id)

    result = filter_chunks_by_permission(
        db,
        "rag_docs",
        [Chunk(text="orphan content", doc_name="orphan.txt", chunk_index=0)],
        viewer,
    )

    assert result == []


def test_file_listing_hides_missing_permission_record(api_env):
    """A non-owner cannot discover an uploaded file that lacks a permission row."""
    client, db, data_dir = api_env
    viewer_id = db.create_user("viewer", "Password2")
    (data_dir / "orphan.txt").write_text("orphan content")

    response = client.get("/files", headers=_headers(viewer_id, "viewer"))

    assert response.status_code == 200, response.text
    assert "orphan.txt" not in {item["name"] for item in response.json()["files"]}
