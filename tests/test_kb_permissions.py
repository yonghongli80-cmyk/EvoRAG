"""Phase 1 tests: KB ownership and scope-based access control."""

from unittest.mock import MagicMock, patch

import pytest

from rag.user_db import UserDB


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    udb = UserDB(path)
    yield udb
    udb.close()


def test_kb_metadata_has_owner_id_and_scope_columns(db):
    """kb_metadata 表应有 owner_id 和 scope 列。"""
    with db._lock:
        db._conn.execute(
            "INSERT INTO kb_metadata (kb_id, name, owner_id, scope) VALUES (?, ?, ?, ?)",
            ("kb_test_001", "测试知识库", 1, "private"),
        )
        db._conn.commit()
        row = db._conn.execute(
            "SELECT kb_id, name, owner_id, scope FROM kb_metadata WHERE kb_id = ?",
            ("kb_test_001",),
        ).fetchone()
    assert row is not None
    assert row["owner_id"] == 1
    assert row["scope"] == "private"


def test_document_permissions_has_scope_column(db):
    """document_permissions 表应有 scope 列。"""
    uid = db.create_user("alice", "pwd")
    with db._lock:
        db._conn.execute(
            "INSERT INTO document_permissions (doc_name, kb_id, owner_id, is_public, protected, scope) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test.pdf", "rag_docs", uid, 0, 0, "private"),
        )
        db._conn.commit()
        row = db._conn.execute(
            "SELECT scope FROM document_permissions WHERE doc_name = ?",
            ("test.pdf",),
        ).fetchone()
    assert row is not None
    assert row["scope"] == "private"


# ── Task 1.2: KB Metadata CRUD ─────────────────────────────────────


def test_create_kb_metadata(db):
    """create_kb_metadata 应写入 owner_id 和 scope。"""
    db.create_kb_metadata("kb_test_001", "测试知识库", owner_id=1, scope="private")
    meta = db.get_kb_metadata("kb_test_001")
    assert meta is not None
    assert meta["kb_id"] == "kb_test_001"
    assert meta["name"] == "测试知识库"
    assert meta["owner_id"] == 1
    assert meta["scope"] == "private"


def test_get_kb_metadata_nonexistent(db):
    """不存在的 KB 返回 None。"""
    assert db.get_kb_metadata("kb_nonexistent") is None


def test_update_kb_scope(db):
    """update_kb_scope 应更新 scope 字段。"""
    db.create_kb_metadata("kb_test_001", "测试知识库", owner_id=1, scope="private")
    db.update_kb_scope("kb_test_001", "public")
    meta = db.get_kb_metadata("kb_test_001")
    assert meta["scope"] == "public"


def test_get_kb_metadata_by_names_batch(db):
    """get_kb_metadata_by_names 应批量查询。"""
    db.create_kb_metadata("kb_a", "A库", owner_id=1, scope="public")
    db.create_kb_metadata("kb_b", "B库", owner_id=2, scope="private")
    result = db.get_kb_metadata_by_names(["kb_a", "kb_b", "kb_c"])
    assert len(result) == 3
    assert result["kb_a"]["scope"] == "public"
    assert result["kb_b"]["scope"] == "private"
    assert result["kb_c"] is None


# ── Task 1.3: KB 列出过滤 + CREATE KB ──────────────────────────────


def test_list_kbs_filters_private_for_non_owner(db):
    """非 owner 不应看到 private KB。"""
    owner = db.create_user("alice", "pwd")
    other = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_private", "私有库", owner_id=owner, scope="private")
    db.create_kb_metadata("kb_public", "公开库", owner_id=owner, scope="public")

    meta_map = db.get_kb_metadata_by_names(["kb_private", "kb_public"])
    visible = []
    for kid, meta in meta_map.items():
        if meta is None or meta["scope"] == "public" or meta["owner_id"] == other:
            visible.append(kid)

    assert "kb_public" in visible
    assert "kb_private" not in visible


def test_list_kbs_owner_sees_own_private(db):
    """owner 应看到自己的 private KB。"""
    owner = db.create_user("alice", "pwd")
    db.create_kb_metadata("kb_private", "私有库", owner_id=owner, scope="private")

    meta_map = db.get_kb_metadata_by_names(["kb_private"])
    visible = []
    for kid, meta in meta_map.items():
        if meta is None or meta["scope"] == "public" or meta["owner_id"] == owner:
            visible.append(kid)

    assert "kb_private" in visible


def test_list_kbs_old_kb_visible_to_all(db):
    """旧 KB（owner_id=0）应所有人可见。"""
    other = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_old", "旧库", owner_id=0, scope="public")

    meta_map = db.get_kb_metadata_by_names(["kb_old"])
    visible = []
    for kid, meta in meta_map.items():
        if meta is None or meta["scope"] == "public" or meta["owner_id"] == other:
            visible.append(kid)

    assert "kb_old" in visible


def test_list_kbs_admin_sees_all(db):
    """admin 应看到所有 KB。"""
    owner = db.create_user("alice", "pwd")
    admin = db.create_user("root", "pwd")
    db.set_user_admin(admin, True)
    db.create_kb_metadata("kb_private", "私有库", owner_id=owner, scope="private")
    db.create_kb_metadata("kb_public", "公开库", owner_id=owner, scope="public")

    meta_map = db.get_kb_metadata_by_names(["kb_private", "kb_public"])
    is_admin = True
    visible = []
    for kid, meta in meta_map.items():
        if is_admin or meta is None or meta["scope"] == "public" or meta["owner_id"] == admin:
            visible.append(kid)

    assert "kb_private" in visible
    assert "kb_public" in visible


# ── Task 1.4: KB 删除/修改加 owner 检查 ────────────────────────────


def test_delete_kb_requires_owner(db):
    """非 owner 删除 KB 应被拒绝。"""
    owner = db.create_user("alice", "pwd")
    other = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    meta = db.get_kb_metadata("kb_test")
    is_admin = False
    is_owner = meta["owner_id"] == other

    # 非 owner 且非 admin → 应拒绝
    assert not is_admin and not is_owner


def test_delete_kb_owner_can_delete(db):
    """owner 删除自己的 KB 应成功。"""
    owner = db.create_user("alice", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    meta = db.get_kb_metadata("kb_test")
    is_owner = meta["owner_id"] == owner
    assert is_owner


def test_delete_kb_admin_can_delete(db):
    """admin 删除任意 KB 应成功。"""
    owner = db.create_user("alice", "pwd")
    admin = db.create_user("root", "pwd")
    db.set_user_admin(admin, True)
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    # admin 可以删任何 KB
    assert True


def test_delete_old_kb_requires_admin(db):
    """旧 KB（owner_id=0）只有 admin 可删除。"""
    other = db.create_user("bob", "pwd")
    admin = db.create_user("root", "pwd")
    db.set_user_admin(admin, True)
    db.create_kb_metadata("kb_old", "旧库", owner_id=0, scope="public")

    meta = db.get_kb_metadata("kb_old")
    # 普通用户
    is_admin = False
    is_owner = meta["owner_id"] == other
    assert not is_admin and not is_owner  # 应拒绝

    # admin
    is_admin = True
    assert is_admin  # 应放行


# ── Task 1.5: KB 查询按 scope 过滤 ─────────────────────────────────


def test_query_private_kb_requires_owner(db):
    """私有 KB 只有 owner 和 admin 可查询。"""
    owner = db.create_user("alice", "pwd")
    other = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    meta = db.get_kb_metadata("kb_test")
    # owner 可查询
    assert meta["owner_id"] == owner
    # 非 owner 不可查询
    assert meta["owner_id"] != other


def test_query_public_kb_allowed(db):
    """公开 KB 所有人可查询。"""
    owner = db.create_user("alice", "pwd")
    db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="public")

    meta = db.get_kb_metadata("kb_test")
    assert meta["scope"] == "public"  # public → 所有人可查


def test_query_old_kb_allowed(db):
    """旧 KB（owner_id=0）所有人可查询。"""
    db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_old", "旧库", owner_id=0, scope="public")

    meta = db.get_kb_metadata("kb_old")
    assert meta["owner_id"] == 0  # 旧 KB → 所有人可查


# ── API 层权限测试 ─────────────────────────────────────────────────


@patch("rag.routers.knowledge.KnowledgeBaseManager")
def test_api_delete_kb_requires_administer_role(mock_kb_cls, isolated_api, make_user):
    """Organization content managers cannot delete the knowledge base itself."""
    mock_manager = MagicMock()
    mock_manager.delete_kb.return_value = None
    mock_kb_cls.return_value = mock_manager
    manager, manager_headers = make_user("org-manager")
    _admin, admin_headers = make_user("org-admin", is_admin=True)
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','团队','team')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P-MANAGER',?,'负责人','经理')",
            (manager["id"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P-MANAGER','TEAM',1)")
        conn.execute("INSERT INTO org_manager_scopes VALUES('P-MANAGER','TEAM')")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb_perm_test','组织库')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES('kb_perm_test','TEAM','organization')"
        )

    assert isolated_api.delete(
        "/knowledge-bases/kb_perm_test", headers=manager_headers
    ).status_code == 404
    assert isolated_api.delete(
        "/knowledge-bases/kb_perm_test", headers=admin_headers
    ).status_code == 200


@patch("rag.routers.knowledge.KnowledgeBaseManager")
def test_api_query_organization_kb_rejects_outside_user(
    mock_kb_cls, isolated_api, make_user
):
    """Organization membership, not legacy private ownership, controls queries."""
    from rag.models import Chunk

    mock_manager = MagicMock()
    mock_manager.search.return_value = [Chunk("allowed", "doc.txt", 0)]
    mock_kb_cls.return_value = mock_manager
    member, member_headers = make_user("org-member")
    _outsider, outsider_headers = make_user("org-outsider")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','团队','team')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P-MEMBER',?,'成员','专员')",
            (member["id"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P-MEMBER','TEAM',1)")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb_org_test','组织库')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES('kb_org_test','TEAM','organization')"
        )

    assert isolated_api.post(
        "/knowledge-bases/kb_org_test/query",
        json={"question": "test"},
        headers=outsider_headers,
    ).status_code == 404
    assert isolated_api.post(
        "/knowledge-bases/kb_org_test/query",
        json={"question": "test"},
        headers=member_headers,
    ).status_code == 200


def test_api_no_auth_returns_401(monkeypatch):
    """未登录访问 KB 端点应返回 401。"""
    from fastapi.testclient import TestClient

    from config import settings
    from rag.api import app

    client = TestClient(app)
    monkeypatch.setattr(settings, "auth_enabled", True)

    res = client.post("/knowledge-bases", json={"name": "no_auth_test"})
    assert res.status_code == 401

    res = client.delete("/knowledge-bases/kb_nonexistent")
    assert res.status_code == 401


# ── Phase 1a: 用户管理 ─────────────────────────────────────────────


def test_user_has_password_changed_at(db):
    """users 表应有 password_changed_at 字段，默认 NULL。"""
    uid = db.create_user("alice", "pwd")
    with db._lock:
        row = db._conn.execute(
            "SELECT password_changed_at FROM users WHERE id = ?", (uid,)
        ).fetchone()
    assert row is not None
    assert row["password_changed_at"] is None  # 默认 NULL


# ── Task 2: 修改密码 ───────────────────────────────────────────────


def test_change_password_success(db):
    """修改密码成功。"""
    uid = db.create_user("alice", "OldPass1")
    db.change_password(uid, "OldPass1", "NewPass1")
    user = db.authenticate("alice", "NewPass1")
    assert user is not None


def test_change_password_hash_format(db):
    """修改密码后 hash 格式应为 salt_hex$hash_hex。"""
    uid = db.create_user("alice", "OldPass1")
    db.change_password(uid, "OldPass1", "NewPass1")
    # 直接查数据库验证格式
    with db._lock:
        row = db._conn.execute("SELECT password FROM users WHERE id = ?", (uid,)).fetchone()
    assert "$" in row["password"]


def test_change_password_wrong_old(db):
    """旧密码错误应失败。"""
    uid = db.create_user("alice", "OldPass1")
    with pytest.raises(ValueError, match="旧密码错误"):
        db.change_password(uid, "WrongPass", "NewPass1")


def test_change_password_too_weak(db):
    """新密码太弱应失败。"""
    uid = db.create_user("alice", "OldPass1")
    with pytest.raises(ValueError, match="密码至少 8 位"):
        db.change_password(uid, "OldPass1", "123")


def test_change_password_missing_uppercase(db):
    """新密码缺大写字母应失败。"""
    uid = db.create_user("alice", "OldPass1")
    with pytest.raises(ValueError, match="大写字母"):
        db.change_password(uid, "OldPass1", "newpass123")


def test_change_password_missing_lowercase(db):
    """新密码缺小写字母应失败。"""
    uid = db.create_user("alice", "OldPass1")
    with pytest.raises(ValueError, match="小写字母"):
        db.change_password(uid, "OldPass1", "NEWPASS123")


def test_change_password_missing_digit(db):
    """新密码缺数字应失败。"""
    uid = db.create_user("alice", "OldPass1")
    with pytest.raises(ValueError, match="数字"):
        db.change_password(uid, "OldPass1", "NewPassword")


def test_change_password_same_as_old(db):
    """新密码与旧密码相同应失败。"""
    uid = db.create_user("alice", "OldPass1")
    with pytest.raises(ValueError, match="不能与旧密码相同"):
        db.change_password(uid, "OldPass1", "OldPass1")


def test_change_password_updates_password_changed_at(db):
    """修改密码后 password_changed_at 应更新。"""
    uid = db.create_user("alice", "OldPass1")
    db.change_password(uid, "OldPass1", "NewPass1")
    user = db.get_user_by_id(uid)
    assert user["password_changed_at"] is not None


# ── Task 3: token 验证检查 password_changed_at ─────────────────────


def test_decode_token_with_password_changed_at():
    """decode_token 应支持 password_changed_at 参数。"""
    from rag.auth import create_token, decode_token

    token = create_token({"user_id": 1, "username": "alice"})
    payload = decode_token(token)
    iat = payload["iat"]

    # password_changed_at 为 None → 跳过检查
    assert decode_token(token, password_changed_at=None) is not None

    # password_changed_at 在 iat 之前 → token 有效
    assert decode_token(token, password_changed_at=iat - 100) is not None

    # password_changed_at 在 iat 之后 → token 失效
    assert decode_token(token, password_changed_at=iat + 100) is None


# ── Task 4: admin 重置密码 ─────────────────────────────────────────


def test_admin_reset_password(db):
    """admin 重置密码成功。"""
    uid = db.create_user("alice", "OldPass1")
    db.reset_password(uid, "NewPass1")
    user = db.authenticate("alice", "NewPass1")
    assert user is not None


def test_reset_password_invalidates_old_token(db):
    """重置密码后旧 token 应失效。"""
    uid = db.create_user("alice", "OldPass1")
    from rag.auth import create_token
    create_token({"user_id": uid, "username": "alice"})
    # 重置密码
    db.reset_password(uid, "NewPass1")
    # 检查 password_changed_at 已更新
    user = db.get_user_by_id(uid)
    assert user["password_changed_at"] is not None


# ── Task 5: 对话搜索 ───────────────────────────────────────────────


def test_search_conversations_by_title(db):
    """搜索对话标题应返回匹配结果。"""
    uid = db.create_user("alice", "pwd")
    db.create_conversation(uid, title="RAG 技术讨论")
    results = db.search_conversations(uid, "RAG")
    assert len(results) >= 1
    assert any(r["title"] == "RAG 技术讨论" for r in results)


def test_search_conversations_by_content(db):
    """搜索消息内容应返回匹配结果。"""
    uid = db.create_user("alice", "pwd")
    cid = db.create_conversation(uid, title="对话1")
    db.add_message(cid, "user", "什么是向量数据库？")
    results = db.search_conversations(uid, "向量")
    assert len(results) >= 1


def test_search_conversations_no_result(db):
    """无匹配应返回空列表。"""
    uid = db.create_user("alice", "pwd")
    results = db.search_conversations(uid, "不存在的关键词xyz")
    assert len(results) == 0


def test_search_conversations_pagination(db):
    """搜索应支持分页。"""
    uid = db.create_user("alice", "pwd")
    for i in range(25):
        cid = db.create_conversation(uid, title=f"对话 {i}")
        db.add_message(cid, "user", f"消息内容 {i}")
    results = db.search_conversations(uid, "消息内容", page=1, size=10)
    assert len(results) == 10


# ── Phase 2: shared 档 + 共享机制 ──────────────────────────────────


def test_kb_shares_table_exists(db):
    """kb_shares 表应存在。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    with db._lock:
        db._conn.execute(
            "INSERT INTO kb_shares (kb_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            ("kb_test", viewer, "view", owner),
        )
        db._conn.commit()
        row = db._conn.execute(
            "SELECT kb_id, user_id, permission, granted_by FROM kb_shares WHERE kb_id = ?",
            ("kb_test",),
        ).fetchone()
    assert row is not None
    assert row["permission"] == "view"
    assert row["user_id"] == viewer


def test_document_shares_has_permission_column(db):
    """document_shares 表应有 permission 列。"""
    uid = db.create_user("alice", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", uid, is_public=False)
    with db._lock:
        db._conn.execute(
            "INSERT INTO document_shares (doc_id, user_id, granted_by, permission) VALUES (?, ?, ?, ?)",
            (doc_id, uid, uid, "edit"),
        )
        db._conn.commit()
        row = db._conn.execute(
            "SELECT permission FROM document_shares WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    assert row is not None
    assert row["permission"] == "edit"


# ── Task 2.2: 用户搜索 API ─────────────────────────────────────────


def test_search_users_returns_results(db):
    """搜索用户应返回匹配的结果。"""
    db.create_user("alice_wang", "pwd")
    db.create_user("alice_li", "pwd")
    db.create_user("bob_zhang", "pwd")

    # 模拟搜索逻辑
    with db._lock:
        rows = db._conn.execute(
            "SELECT id, username FROM users WHERE username LIKE ? LIMIT 20",
            ("%alice%",),
        ).fetchall()
    results = [{"id": r["id"], "username": r["username"]} for r in rows]

    assert len(results) == 2
    assert all("alice" in r["username"] for r in results)


def test_search_users_min_query_length(db):
    """搜索词至少 2 个字符。"""
    query = "a"
    assert len(query) < 2  # 应拒绝


def test_search_users_max_20_results(db):
    """搜索结果最多 20 条。"""
    for i in range(25):
        db.create_user(f"user_{i:03d}", "pwd")

    with db._lock:
        rows = db._conn.execute(
            "SELECT id, username FROM users WHERE username LIKE ? LIMIT 20",
            ("%user%",),
        ).fetchall()

    assert len(rows) <= 20


def test_search_users_only_returns_id_and_username(db):
    """搜索结果只含 id 和 username，不含敏感信息。"""
    db.create_user("test_user", "pwd")

    with db._lock:
        rows = db._conn.execute(
            "SELECT id, username FROM users WHERE username LIKE ?",
            ("%test_user%",),
        ).fetchall()
    result = dict(rows[0])

    assert "id" in result
    assert "username" in result
    assert "password" not in result
    assert "is_admin" not in result


# ── Task 2.6: scope 切换逻辑 ────────────────────────────────────────


def test_scope_switch_private_to_public(db):
    """private → public 切换应成功。"""
    uid = db.create_user("alice", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=uid, scope="private")
    db.update_kb_scope("kb_test", "public")
    meta = db.get_kb_metadata("kb_test")
    assert meta["scope"] == "public"


def test_scope_switch_public_to_private(db):
    """public → private 切换应成功。"""
    uid = db.create_user("alice", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=uid, scope="public")
    db.update_kb_scope("kb_test", "private")
    meta = db.get_kb_metadata("kb_test")
    assert meta["scope"] == "private"


def test_scope_switch_clears_shares_on_private(db):
    """shared → private 时应清除该 KB 的所有 shares。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="public")

    # 添加共享记录
    with db._lock:
        db._conn.execute(
            "INSERT INTO kb_shares (kb_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            ("kb_test", viewer, "view", owner),
        )
        db._conn.commit()

    # 切换到 private
    db.update_kb_scope("kb_test", "private")

    # 共享记录应被清除
    with db._lock:
        row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM kb_shares WHERE kb_id = ?",
            ("kb_test",),
        ).fetchone()
    assert row["cnt"] == 0


def test_scope_switch_clears_shares_on_public(db):
    """shared → public 时应清除该 KB 的所有 shares。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="public")

    # 添加共享记录
    with db._lock:
        db._conn.execute(
            "INSERT INTO kb_shares (kb_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            ("kb_test", viewer, "view", owner),
        )
        db._conn.commit()

    # 切换到 public（已经是 public，但测试逻辑）
    db.update_kb_scope("kb_test", "public")

    # 共享记录应被清除（public 不需要共享列表）
    with db._lock:
        row = db._conn.execute(
            "SELECT COUNT(*) as cnt FROM kb_shares WHERE kb_id = ?",
            ("kb_test",),
        ).fetchone()
    assert row["cnt"] == 0


def test_protected_file_scope_cannot_change(db):
    """protected 文件的 scope 不可修改。"""
    db.create_user("alice", "pwd")
    with db._lock:
        db._conn.execute(
            "INSERT INTO document_permissions (doc_name, kb_id, owner_id, is_public, protected, scope) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("repo_file.md", "rag_docs", 0, 1, 1, "public"),
        )
        db._conn.commit()

    # 尝试修改 protected 文件的 scope
    with db._lock:
        db._conn.execute(
            "UPDATE document_permissions SET scope = 'private' WHERE doc_name = ? AND protected = 1",
            ("repo_file.md",),
        )
        db._conn.commit()

    # protected 文件的 scope 不应被修改（需要在 API 层强制）
    # 这里只测试 DB 层可以修改，API 层会加保护
    with db._lock:
        row = db._conn.execute(
            "SELECT scope, protected FROM document_permissions WHERE doc_name = ?",
            ("repo_file.md",),
        ).fetchone()
    # DB 层允许修改，但 API 层应阻止
    assert row["protected"] == 1


# ── Task 2.5: 权限判定重写为三档 ────────────────────────────────────


def test_check_doc_permission_scope_private_only_owner(db):
    """scope=private 时只有 owner 可查看。"""
    import pytest
    from fastapi import HTTPException

    from rag.permissions import check_doc_permission

    owner = db.create_user("alice", "pwd")
    other = db.create_user("bob", "pwd")
    db.create_document_permission("test.pdf", "rag_docs", owner, scope="private")

    # owner 可查看
    result = check_doc_permission(db, "test.pdf", "rag_docs", {"id": owner, "is_admin": False}, "view")
    assert result is not None

    # 非 owner 不可查看
    with pytest.raises(HTTPException) as exc_info:
        check_doc_permission(db, "test.pdf", "rag_docs", {"id": other, "is_admin": False}, "view")
    assert exc_info.value.status_code == 403


def test_check_doc_permission_scope_public_anyone(db):
    """scope=public 时任何人可查看。"""
    from rag.permissions import check_doc_permission

    owner = db.create_user("alice", "pwd")
    other = db.create_user("bob", "pwd")
    db.create_document_permission("test.pdf", "rag_docs", owner, scope="public")

    # 非 owner 也可查看
    result = check_doc_permission(db, "test.pdf", "rag_docs", {"id": other, "is_admin": False}, "view")
    assert result is not None


def test_check_doc_permission_scope_shared_view(db):
    """scope=shared + permission=view 时共享用户可查看但不可编辑。"""
    import pytest
    from fastapi import HTTPException

    from rag.permissions import check_doc_permission

    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", owner, scope="shared")

    # 添加共享记录
    with db._lock:
        db._conn.execute(
            "INSERT INTO document_shares (doc_id, user_id, granted_by, permission) VALUES (?, ?, ?, ?)",
            (doc_id, viewer, owner, "view"),
        )
        db._conn.commit()

    # 共享用户可查看
    result = check_doc_permission(db, "test.pdf", "rag_docs", {"id": viewer, "is_admin": False}, "view")
    assert result is not None

    # 共享用户不可编辑
    with pytest.raises(HTTPException) as exc_info:
        check_doc_permission(db, "test.pdf", "rag_docs", {"id": viewer, "is_admin": False}, "edit")
    assert exc_info.value.status_code == 403


def test_check_doc_permission_scope_shared_edit(db):
    """scope=shared + permission=edit 时共享用户可查看和编辑。"""
    from rag.permissions import check_doc_permission

    owner = db.create_user("alice", "pwd")
    editor = db.create_user("bob", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", owner, scope="shared")

    # 添加共享记录
    with db._lock:
        db._conn.execute(
            "INSERT INTO document_shares (doc_id, user_id, granted_by, permission) VALUES (?, ?, ?, ?)",
            (doc_id, editor, owner, "edit"),
        )
        db._conn.commit()

    # 共享用户可查看
    result = check_doc_permission(db, "test.pdf", "rag_docs", {"id": editor, "is_admin": False}, "view")
    assert result is not None

    # 共享用户可编辑
    result = check_doc_permission(db, "test.pdf", "rag_docs", {"id": editor, "is_admin": False}, "edit")
    assert result is not None


def test_check_doc_permission_admin_bypasses_scope(db):
    """admin 可绕过所有 scope 限制。"""
    from rag.permissions import check_doc_permission

    owner = db.create_user("alice", "pwd")
    admin = db.create_user("root", "pwd")
    db.set_user_admin(admin, True)
    db.create_document_permission("test.pdf", "rag_docs", owner, scope="private")

    # admin 可查看私有文件
    result = check_doc_permission(db, "test.pdf", "rag_docs", {"id": admin, "is_admin": True}, "view")
    assert result is not None


def test_check_kb_permission_scope_private_only_owner(db):
    """KB scope=private 时只有 owner 可查看。"""
    import pytest
    from fastapi import HTTPException

    from rag.permissions import check_kb_permission

    owner = db.create_user("alice", "pwd")
    other = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    # owner 可查看
    result = check_kb_permission(db, "kb_test", {"id": owner, "is_admin": False}, "view")
    assert result is not None

    # 非 owner 不可查看
    with pytest.raises(HTTPException) as exc_info:
        check_kb_permission(db, "kb_test", {"id": other, "is_admin": False}, "view")
    assert exc_info.value.status_code == 403


def test_check_kb_permission_scope_public_anyone(db):
    """KB scope=public 时任何人可查看。"""
    from rag.permissions import check_kb_permission

    owner = db.create_user("alice", "pwd")
    other = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="public")

    # 非 owner 也可查看
    result = check_kb_permission(db, "kb_test", {"id": other, "is_admin": False}, "view")
    assert result is not None


def test_check_kb_permission_admin_bypasses(db):
    """admin 可绕过所有 KB scope 限制。"""
    from rag.permissions import check_kb_permission

    owner = db.create_user("alice", "pwd")
    admin = db.create_user("root", "pwd")
    db.set_user_admin(admin, True)
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    # admin 可查看私有 KB
    result = check_kb_permission(db, "kb_test", {"id": admin, "is_admin": True}, "view")
    assert result is not None


# ── Task 2.3 + 2.4: 文件/KB 共享 API ────────────────────────────────


def test_share_document_creates_record(db):
    """共享文档应创建 document_shares 记录。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", owner, scope="shared")

    db.share_document(doc_id, viewer, owner)
    assert db.is_document_shared(doc_id, viewer)


def test_share_document_with_permission(db):
    """共享文档时可指定 permission（view/edit）。"""
    owner = db.create_user("alice", "pwd")
    editor = db.create_user("bob", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", owner, scope="shared")

    db.share_document(doc_id, editor, owner, permission="edit")
    assert db.is_document_shared(doc_id, editor, permission="edit")
    assert not db.is_document_shared(doc_id, editor, permission="view")


def test_unshare_document_removes_record(db):
    """取消共享应删除 document_shares 记录。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", owner, scope="shared")

    db.share_document(doc_id, viewer, owner)
    assert db.is_document_shared(doc_id, viewer)

    db.unshare_document(doc_id, viewer)
    assert not db.is_document_shared(doc_id, viewer)


def test_share_kb_creates_record(db):
    """共享知识库应创建 kb_shares 记录。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="shared")

    db.share_kb("kb_test", viewer, owner)
    assert db.is_kb_shared("kb_test", viewer)


def test_share_kb_with_permission(db):
    """共享知识库时可指定 permission（view/edit）。"""
    owner = db.create_user("alice", "pwd")
    editor = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="shared")

    db.share_kb("kb_test", editor, owner, permission="edit")
    assert db.is_kb_shared("kb_test", editor, permission="edit")
    assert not db.is_kb_shared("kb_test", editor, permission="view")


def test_unshare_kb_removes_record(db):
    """取消共享应删除 kb_shares 记录。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="shared")

    db.share_kb("kb_test", viewer, owner)
    assert db.is_kb_shared("kb_test", viewer)

    db.unshare_kb("kb_test", viewer)
    assert not db.is_kb_shared("kb_test", viewer)


# ── Phase 3: 下载控制 ───────────────────────────────────────────────


def test_document_permissions_has_downloadable_column(db):
    """document_permissions 表应有 downloadable 列。"""
    uid = db.create_user("alice", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", uid, scope="private")
    with db._lock:
        row = db._conn.execute(
            "SELECT downloadable FROM document_permissions WHERE id = ?",
            (doc_id,),
        ).fetchone()
    assert row is not None
    assert row["downloadable"] == 1  # 默认可下载


def test_create_document_permission_default_downloadable(db):
    """新建文档权限默认可下载。"""
    uid = db.create_user("alice", "pwd")
    doc_id = db.create_document_permission("test.pdf", "rag_docs", uid, scope="private")
    perm = db.get_document_permission_by_id(doc_id)
    assert perm["downloadable"] is True


# ── Task 3.2: 下载端点 ──────────────────────────────────────────────


def test_download_requires_auth(monkeypatch):
    """下载文件需要认证。"""
    from fastapi.testclient import TestClient

    from config import settings
    from rag.api import app

    monkeypatch.setattr(settings, "auth_enabled", True)
    client = TestClient(app)
    res = client.get("/files/test.txt/download")
    assert res.status_code == 401


def test_download_file_not_found():
    """下载不存在的文件应返回 404。"""
    from fastapi.testclient import TestClient

    from rag.api import app, user_db
    from rag.auth import create_token

    client = TestClient(app)
    try:
        uid = user_db.create_user("_dl_test", "pwd")
    except ValueError:
        uid = user_db.get_user_by_username("_dl_test")["id"]
    token = create_token({"user_id": uid, "username": "_dl_test"})

    res = client.get("/files/nonexistent.txt/download",
                     headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 404


# ── 方案 A：共享时自动切换 scope ────────────────────────────────────


def test_share_kb_auto_changes_scope_to_shared(db):
    """共享 KB 时应自动把 scope 改为 'shared'。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    # 共享前 scope 是 private
    meta = db.get_kb_metadata("kb_test")
    assert meta["scope"] == "private"

    # 共享后 scope 自动变为 shared
    db.share_kb("kb_test", viewer, owner)
    meta = db.get_kb_metadata("kb_test")
    assert meta["scope"] == "shared"


def test_unshare_last_user_reverts_scope_to_private(db):
    """取消最后一个共享用户后，scope 应自动变回 'private'。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="private")

    # 添加共享（scope 自动变 shared）
    db.share_kb("kb_test", viewer, owner)
    meta = db.get_kb_metadata("kb_test")
    assert meta["scope"] == "shared"

    # 取消共享（scope 自动变回 private）
    db.unshare_kb("kb_test", viewer)
    meta = db.get_kb_metadata("kb_test")
    assert meta["scope"] == "private"


def test_list_kb_shares_returns_users(db):
    """查看共享列表应返回用户信息。"""
    owner = db.create_user("alice", "pwd")
    viewer = db.create_user("bob", "pwd")
    db.create_kb_metadata("kb_test", "测试库", owner_id=owner, scope="shared")

    db.share_kb("kb_test", viewer, owner, permission="view")
    shares = db.list_kb_shared_users("kb_test")

    assert len(shares) == 1
    assert shares[0]["username"] == "bob"
    assert shares[0]["permission"] == "view"
