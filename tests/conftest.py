import os

# 安全修复后 config.auth_enabled 默认开启（True）。
# 既有测试大多不带 token 直接调用接口，依赖认证关闭环境运行，
# 因此在本文件被 pytest 加载时（早于任何 import config / import rag.*）
# 把 RAG_AUTH_ENABLED 预置为 "false"。
# tests/test_auth_default_enabled.py 用 monkeypatch.delenv 恢复未设置态来测默认值。
os.environ.setdefault("RAG_AUTH_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from rag.auth import create_token
from rag.user_db import UserDB


@pytest.fixture(autouse=True)
def reset_vector_store():
    import contextlib

    import rag.vector_store as vs
    with contextlib.suppress(Exception):
        vs._client.close() if vs._client is not None else None
    vs._client = None
    # Clear BM25 module-level cache to prevent test pollution
    with contextlib.suppress(Exception):
        import sys
        if "rag.retriever" in sys.modules:
            sys.modules["rag.retriever"]._bm25_cache.clear()
    yield
    with contextlib.suppress(Exception):
        vs._client.close() if vs._client is not None else None
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
def mock_embedding_indexing(monkeypatch):
    """Replace the embedding model and vector writes so ingest tests run offline.

    The local machine has no model API credentials, and tests must never write
    to the repository ``qdrant_data``. This keeps the versioned ingest service
    fully exercised while stubbing only the external embedding/write boundary.
    """
    import rag.embedder
    import rag.vector_store

    monkeypatch.setattr(
        rag.embedder, "embed", lambda texts: [[0.1] * 1024 for _ in texts]
    )
    monkeypatch.setattr(
        rag.vector_store, "add_to_collection", lambda *args, **kwargs: None
    )


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    """Give API tests a private database and upload root."""
    import rag.api as api
    from config import settings

    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    upload_dir = tmp_path / "upload"
    kb_files_root = tmp_path / "kb_files"
    document_versions_root = tmp_path / "document_versions"
    previous_db = api.user_db
    previous_state_db = getattr(api.app.state, "user_db", None)
    previous_upload_dir = getattr(api.app.state, "upload_dir", None)
    previous_kb_files_root = getattr(api.app.state, "kb_files_root", None)
    previous_document_versions_root = getattr(api.app.state, "document_versions_root", None)

    monkeypatch.setattr(api, "user_db", db)
    monkeypatch.setattr(api, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(api, "DATA_DIR", upload_dir)
    monkeypatch.setattr(api, "KB_FILES_ROOT", kb_files_root, raising=False)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "document_versions_root", str(document_versions_root), raising=False)
    api.app.state.user_db = db
    api.app.state.upload_dir = upload_dir
    api.app.state.kb_files_root = kb_files_root
    api.app.state.document_versions_root = document_versions_root

    with TestClient(api.app) as client:
        yield client

    api.user_db = previous_db
    if previous_state_db is None:
        del api.app.state.user_db
    else:
        api.app.state.user_db = previous_state_db
    if previous_upload_dir is None:
        del api.app.state.upload_dir
    else:
        api.app.state.upload_dir = previous_upload_dir
    if previous_kb_files_root is None:
        del api.app.state.kb_files_root
    else:
        api.app.state.kb_files_root = previous_kb_files_root
    if previous_document_versions_root is None:
        del api.app.state.document_versions_root
    else:
        api.app.state.document_versions_root = previous_document_versions_root
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
