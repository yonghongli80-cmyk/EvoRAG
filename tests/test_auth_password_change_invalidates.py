import time

import pytest
from fastapi import HTTPException

from config import settings
from rag.auth import create_token, verify_api_key


def test_verify_api_key_rejects_token_issued_before_password_change(monkeypatch):
    import rag.api as api

    monkeypatch.setattr(settings, "auth_enabled", True)
    token = create_token({"user_id": 42, "iat": time.time() - 10})
    monkeypatch.setattr(
        api.user_db,
        "get_user_by_id",
        lambda _user_id: {"id": 42, "password_changed_at": time.time()},
    )

    with pytest.raises(HTTPException, match="认证失败"):
        verify_api_key(api_key=None, authorization=f"Bearer {token}")
