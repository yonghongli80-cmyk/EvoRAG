from config import settings
from rag.auth import verify_api_key


def test_auth_keys_use_user_id_to_api_key_mapping(monkeypatch, sandbox_api_storage):
    """A configured API key resolves to a real, existing principal (B-04).

    The legacy verifier used to return the configured name verbatim without
    touching the database, so a key for a deleted/inactive account still
    authenticated. It now goes through ``resolve_principal``.
    """
    db = sandbox_api_storage
    user_id = db.create_user("user1", "Password-123!")
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_keys", '{"user1": "key-123"}')

    assert verify_api_key(api_key="key-123", authorization="") == str(user_id)
