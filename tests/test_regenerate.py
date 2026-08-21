"""重新生成测试。"""
import os
import tempfile


def test_user_db_update_message():
    """UserDB.update_message 应该更新消息内容。"""
    from rag.user_db import UserDB

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = UserDB(db_path)
        user_id = db.create_user("test", "password123")
        conv_id = db.create_conversation(user_id)
        db.add_message(conv_id, "user", "什么是 mTLS？")
        db.add_message(conv_id, "assistant", "mTLS 是双向 TLS...")

        messages = db.get_messages(conv_id, user_id)
        original_count = len(messages)
        assistant_msg_id = messages[-1]["id"]

        db.update_message(assistant_msg_id, "mTLS（双向 TLS）是一种安全协议...")

        messages_after = db.get_messages(conv_id, user_id)
        assert len(messages_after) == original_count
        assert messages_after[-1]["content"] == "mTLS（双向 TLS）是一种安全协议..."
    finally:
        db.close()
        os.unlink(db_path)


def test_regenerate_endpoint_exists():
    """POST /regenerate 端点应该存在。"""
    from fastapi.testclient import TestClient

    from rag.api import app
    client = TestClient(app)
    response = client.post("/regenerate", json={"conversation_id": 1, "message_id": 1})
    assert response.status_code != 404
