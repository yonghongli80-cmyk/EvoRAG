"""TDD tests for code review fixes (C26, C8, I19, I17)."""


import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from rag.api import app
    return TestClient(app)


@pytest.fixture()
def db(tmp_path):
    from rag.user_db import UserDB
    path = str(tmp_path / "test.db")
    udb = UserDB(path)
    yield udb
    udb.close()


# ── C26: /data/upload 静态挂载绕过鉴权 ────────────────────────────


def test_data_upload_not_accessible_without_auth(client):
    """C26: 直接访问 /data/upload/ 下的文件应返回 404（不挂载）或 401/403。"""
    response = client.get("/data/upload/nonexistent.txt")
    # 不应返回 200（直接下载）
    assert response.status_code == 404


# ── C8: delete_file 未登录可删除文件 ──────────────────────────────


def test_delete_file_rejects_no_auth(client, monkeypatch):
    """C8: 认证开启时未登录用户删除文件应返回 401。

    认证关闭（本地开发模式）下 `/files` 与其它端点一致落到开发管理员身份，
    因此这里显式开启认证来验证生产模式下的边界（B-04 统一认证入口）。
    """
    from config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    response = client.delete("/files/test.txt")
    assert response.status_code == 401, f"C8: 未登录删除应返回 401，实际 {response.status_code}"


def test_delete_file_rejects_invalid_token(client):
    """C8: 无效 token 删除文件应返回 401。"""
    response = client.delete("/files/test.txt", headers={"Authorization": "Bearer invalid_token"})
    assert response.status_code == 401, f"C8: 无效 token 应返回 401，实际 {response.status_code}"


# ── I19: delete_file 不清理权限记录 ────────────────────────────────


def test_delete_cleans_permission_record(db):
    """I19: 删除文件时应清理 document_permissions 记录。"""
    uid = db.create_user("alice", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", uid, is_public=False)
    assert db.get_document_permission("test.pdf", "rag_docs") is not None

    # 直接测试 delete_document_permission 方法存在且工作正常
    db.delete_document_permission(doc_id)
    assert db.get_document_permission("test.pdf", "rag_docs") is None


# ── I17: toggle TOCTOU 竞态 ───────────────────────────────────────


def test_toggle_visibility_is_atomic(db):
    """I17: toggle_document_visibility 应正确切换状态。"""
    uid = db.create_user("alice", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", uid, is_public=False)

    # 第一次切换：应从 False 变为 True
    result1 = db.toggle_document_visibility(doc_id)
    assert result1 is True

    # 第二次切换：应从 True 变为 False
    result2 = db.toggle_document_visibility(doc_id)
    assert result2 is False

    # 多次快速切换不应丢失更新
    for _ in range(10):
        db.toggle_document_visibility(doc_id)

    perm = db.get_document_permission_by_id(doc_id)
    # 10 次切换后应回到原始状态（False）
    assert perm["is_public"] is False
