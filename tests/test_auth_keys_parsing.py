from config import settings
from rag.auth import verify_api_key


def test_auth_keys_use_user_id_to_api_key_mapping(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_keys", '{"user1": "key-123"}')

    assert verify_api_key(api_key="key-123", authorization="") == "user1"
