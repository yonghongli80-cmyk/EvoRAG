"""上传强制认证、无权限记录默认拒绝、同名覆盖保护。

覆盖 Task 1.6 安全边界：
- 认证开启时，无 token 上传被拒（401/403）。
- 无权限记录的旧文件：非 owner（任何登录用户）delete/download 被拒。
- 同名已存在文件：非 owner 且文件非 public 时覆盖被拒（409/403）。
- 认证关闭时：匿名上传仍可用（向后兼容）。
- 正常路径：owner 上传 → 可下载 → 可删除。
"""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import rag.api as api_module
from config import settings


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    """隔离 DATA_DIR / user_db，mock 索引与管线；返回 (TestClient, UserDB)。"""
    from rag.user_db import UserDB

    db = UserDB(str(tmp_path / "users.db"))
    monkeypatch.setattr(api_module, "user_db", db)
    monkeypatch.setattr(api_module, "DATA_DIR", tmp_path)

    with ExitStack() as stack:
        mock_kb_cls = stack.enter_context(patch("rag.api.KnowledgeBaseManager"))
        stack.enter_context(patch("rag.pipeline.RAGPipeline"))
        stack.enter_context(patch("rag.vector_store.delete_doc"))
        mock_manager = MagicMock()
        mock_manager.add_document.return_value = 1
        mock_kb_cls.return_value = mock_manager
        client = TestClient(api_module.app)
        yield client, db

    db.close()


def _register(client, username, password="Secret123"):
    """通过 /register 创建用户并返回带 Bearer token 的 headers。"""
    resp = client.post("/register", json={"username": username, "password": password})
    assert resp.status_code == 200, f"register {username} 失败: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _upload(client, headers, filename, content=b"hello"):
    return client.post(
        "/upload",
        files={"file": (filename, content, "text/plain")},
        headers=headers,
    )


# ── 认证开启：强制登录 ──────────────────────────────────────────────


def test_upload_without_token_401_when_auth_enabled(api_env, monkeypatch):
    """认证开启时，无 token 上传返回 401/403，且不写盘。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", True)
    resp = _upload(client, {}, "noauth.txt")
    assert resp.status_code in (401, 403), (
        f"认证开启时无 token 上传应被拒，实际 {resp.status_code}: {resp.text}"
    )
    assert not (api_module.DATA_DIR / "noauth.txt").exists()


def test_upload_with_token_ok_and_creates_record_when_auth_enabled(api_env, monkeypatch):
    """认证开启时，带 token 上传成功且创建私有权限记录。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", True)
    headers = _register(client, "alice")
    resp = _upload(client, headers, "alice.txt", b"alice content")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "uploaded"
    perm = db.get_document_permission("alice.txt", "rag_docs")
    assert perm is not None, "认证开启上传应创建权限记录"
    assert perm["owner_id"] == db.get_user_by_username("alice")["id"]
    assert perm["is_public"] is False


# ── 认证开启：无权限记录文档默认拒绝 ────────────────────────────────


def test_no_record_doc_delete_denied_when_auth_enabled(api_env, monkeypatch):
    """无权限记录的文件，登录用户（非 admin）删除被拒。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", True)
    (api_module.DATA_DIR / "orphan.txt").write_bytes(b"orphan data")
    headers = _register(client, "alice")
    resp = client.delete("/files/orphan.txt", headers=headers)
    assert resp.status_code in (403, 409), (
        f"无记录文件删除应被拒，实际 {resp.status_code}: {resp.text}"
    )
    assert (api_module.DATA_DIR / "orphan.txt").exists(), "拒绝后文件不应被删除"


def test_no_record_doc_download_denied_when_auth_enabled(api_env, monkeypatch):
    """无权限记录的文件，登录用户下载被拒（无法证明公开）。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", True)
    (api_module.DATA_DIR / "orphan.txt").write_bytes(b"orphan data")
    headers = _register(client, "alice")
    resp = client.get("/files/orphan.txt/download", headers=headers)
    assert resp.status_code == 403, (
        f"无记录文件下载应被拒，实际 {resp.status_code}: {resp.text}"
    )


# ── 认证开启：同名覆盖保护 ──────────────────────────────────────────


def test_overwrite_same_name_non_owner_rejected_when_auth_enabled(api_env, monkeypatch):
    """同名已存在文件，非 owner 且非 public 时上传被拒，且不覆盖原文件。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", True)
    headers_a = _register(client, "alice")
    resp = _upload(client, headers_a, "doc.txt", b"alice content")
    assert resp.status_code == 200, resp.text

    headers_b = _register(client, "bob")
    resp = _upload(client, headers_b, "doc.txt", b"bob content")
    assert resp.status_code in (409, 403), (
        f"非 owner 覆盖应被拒，实际 {resp.status_code}: {resp.text}"
    )
    assert (api_module.DATA_DIR / "doc.txt").read_bytes() == b"alice content", (
        "原文件不应被覆盖"
    )


def test_overwrite_same_name_owner_allowed_when_auth_enabled(api_env, monkeypatch):
    """同名已存在文件，owner 重新上传被允许。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", True)
    headers = _register(client, "alice")
    resp = _upload(client, headers, "doc.txt", b"v1")
    assert resp.status_code == 200, resp.text
    resp = _upload(client, headers, "doc.txt", b"v2")
    assert resp.status_code == 200, resp.text
    assert (api_module.DATA_DIR / "doc.txt").read_bytes() == b"v2"


# ── 认证开启：正常路径 ──────────────────────────────────────────────


def test_owner_upload_download_delete_when_auth_enabled(api_env, monkeypatch):
    """正常路径：owner 上传 → 可下载 → 可删除，删除后清理权限记录。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", True)
    headers = _register(client, "alice")
    resp = _upload(client, headers, "owner.txt", b"owner content")
    assert resp.status_code == 200, resp.text

    resp = client.get("/files/owner.txt/download", headers=headers)
    assert resp.status_code == 200, resp.text

    resp = client.delete("/files/owner.txt", headers=headers)
    assert resp.status_code == 200, f"owner 删除失败: {resp.text}"
    assert not (api_module.DATA_DIR / "owner.txt").exists()
    assert db.get_document_permission("owner.txt", "rag_docs") is None


# ── 认证关闭：匿名上传兼容 ──────────────────────────────────────────


def test_anonymous_upload_ok_when_auth_disabled(api_env, monkeypatch):
    """认证关闭时，匿名上传仍可用（向后兼容）。"""
    client, db = api_env
    monkeypatch.setattr(settings, "auth_enabled", False)
    resp = _upload(client, {}, "anon.txt", b"anon content")
    assert resp.status_code == 200, (
        f"认证关闭时匿名上传应成功，实际 {resp.status_code}: {resp.text}"
    )
    assert resp.json()["status"] == "uploaded"
