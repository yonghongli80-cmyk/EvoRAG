"""Tests for /query message saving and /feedback endpoint."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from rag.user_db import UserDB


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Yield a TestClient with a temporary UserDB patched in."""
    db_path = str(tmp_path / "test_users.db")
    import rag.api as api_mod
    from config import settings

    original_db = api_mod.user_db
    original_state_db = getattr(api_mod.app.state, "user_db", None)
    test_db = UserDB(db_path)
    api_mod.user_db = test_db
    api_mod.app.state.user_db = test_db
    monkeypatch.setattr(settings, "auth_enabled", True)

    # Mock the pipeline so queries work without a real index
    mock_pipeline = MagicMock()
    mock_pipeline.query.return_value = SimpleNamespace(
        answer="mock answer",
        sources=[{"doc": "test.txt", "text": "snippet"}],
    )

    with TestClient(api_mod.app) as c:
        # Set mock AFTER startup event (auto_index_on_startup) to prevent
        # it from overwriting our mock with a real RAGPipeline.
        api_mod.pipeline = mock_pipeline
        yield c

    api_mod.pipeline = None
    api_mod.user_db = original_db
    api_mod.app.state.user_db = original_state_db
    test_db.close()


def _register_and_login(client, username="test_user", password="Test_pass_123"):
    """Helper: register a user and return the JWT token."""
    client.post("/register", json={"username": username, "password": password})
    resp = client.post("/login", json={"username": username, "password": password})
    return resp.json()["token"]


def test_query_saves_messages(client):
    """POST /query with conversation_id should save user + assistant messages."""
    token = _register_and_login(client, "query_saver", "Pass12345")
    headers = {"Authorization": f"Bearer {token}"}

    # Create a conversation
    conv_resp = client.post("/conversations", headers=headers)
    assert conv_resp.status_code == 200
    cid = conv_resp.json()["id"]

    # Query with conversation_id
    query_resp = client.post(
        "/query",
        json={"question": "What is RAG?", "conversation_id": cid},
        headers=headers,
    )
    assert query_resp.status_code == 200
    assert query_resp.json()["answer"] == "mock answer"

    # Verify messages were saved
    msgs_resp = client.get(f"/conversations/{cid}/messages", headers=headers)
    assert msgs_resp.status_code == 200
    messages = msgs_resp.json()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "What is RAG?"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "mock answer"


def test_submit_feedback(client):
    """POST /feedback should return 200 and record feedback."""
    token = _register_and_login(client, "feedback_user", "Pass12345")
    headers = {"Authorization": f"Bearer {token}"}

    # Create conversation and add a message directly so we have a message_id
    import rag.api as api_mod

    cid = api_mod.user_db.create_conversation(1)  # user_id=1 from registration
    msg_id = api_mod.user_db.add_message(cid, "assistant", "some answer")

    resp = client.post(
        "/feedback",
        json={"message_id": msg_id, "value": "positive", "comment": "great answer"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
