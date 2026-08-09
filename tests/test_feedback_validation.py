import pytest

from rag.user_db import UserDB


def test_add_feedback_rejects_invalid_value_and_unowned_message(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("alice", "StrongPass1")
    conversation_id = db.create_conversation(user_id)
    message_id = db.add_message(conversation_id, "assistant", "answer")

    with pytest.raises(ValueError, match="value"):
        db.add_feedback(message_id, user_id, 2)
    with pytest.raises(ValueError, match="无权"):
        db.add_feedback(message_id, user_id + 1, 1)
