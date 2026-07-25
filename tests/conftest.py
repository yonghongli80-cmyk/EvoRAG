import pytest
from fastapi.testclient import TestClient

from rag.auth import create_token
from rag.user_db import UserDB


@pytest.fixture(autouse=True)
def reset_vector_store():
    import rag.vector_store as vs
    if vs._client is not None:
        try:
            vs._client.close()
        except Exception:
            pass
    vs._client = None
    # Clear BM25 module-level cache to prevent test pollution
    try:
        import sys
        if "rag.retriever" in sys.modules:
            sys.modules["rag.retriever"]._bm25_cache.clear()
    except Exception:
        pass
    yield
    if vs._client is not None:
        try:
            vs._client.close()
        except Exception:
            pass
    vs._client = None


@pytest.fixture(autouse=True)
def reset_api_pipeline():
    import rag.api
    rag.api.pipeline = None
    yield


@pytest.fixture(autouse=True)
def reset_stream_state():
    """重置流式相关全局状态，防止测试间污染。"""
    import rag.generator as gen
    yield
    gen._async_client = None
    gen._breaker._failure_count = 0
    gen._breaker.state = "closed"
    gen._breaker._probe_admitted = False


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    """Give API tests a private database and upload root."""
    import rag.api as api
    from config import settings

    db = UserDB(str(tmp_path / "users.db"))
    upload_dir = tmp_path / "upload"
    previous_db = api.user_db
    previous_state_db = getattr(api.app.state, "user_db", None)

    monkeypatch.setattr(api, "user_db", db)
    monkeypatch.setattr(api, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(api, "DATA_DIR", upload_dir)
    monkeypatch.setattr(settings, "auth_enabled", True)
    api.app.state.user_db = db

    with TestClient(api.app) as client:
        yield client

    api.user_db = previous_db
    if previous_state_db is None:
        del api.app.state.user_db
    else:
        api.app.state.user_db = previous_state_db
    db.close()


@pytest.fixture
def make_user(isolated_api):
    import rag.api as api

    def _make(username: str, is_admin: bool = False):
        user_id = api.app.state.user_db.create_user(username, "Password-123!")
        if is_admin:
            api.app.state.user_db.set_user_admin(user_id, True)
        user = api.app.state.user_db.get_user_by_id(user_id)
        token = create_token({"user_id": user_id, "username": username})
        return user, {"Authorization": f"Bearer {token}"}

    return _make
