"""TDD tests: 数据源按用户归属隔离、config 脱敏、禁止非只读 SQL。

对应 task-1.4：
- 用户 B 不能 list/get/delete/sync 用户 A 的数据源
- ``list_data_sources`` 返回的 config 不含密码字段明文（含 *** 掩码 / has_secret）
- ``test_connection`` 只验证连通性（SELECT 1），不执行用户 SQL
- sync/执行路径拒绝非 SELECT 语句
- 旧数据（无 owner_id）迁移后仍可访问（owner 默认 admin）
"""

import asyncio
import json
import sqlite3

import pytest

from rag.data_sources.db_source import DBSource, validate_read_only_sql
from rag.user_db import UserDB, mask_data_source_config


def _run(coro):
    """Run an async coroutine in a new event loop (for sync test functions)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def db(tmp_path):
    """Yield a fresh UserDB backed by a temporary SQLite file."""
    path = str(tmp_path / "users.db")
    udb = UserDB(path)
    yield udb
    udb.close()


# ── 归属隔离 ────────────────────────────────────────────────────────────


class TestOwnershipIsolation:
    def test_user_b_cannot_list_user_a_sources(self, db):
        db.create_data_source(
            name="A-source",
            source_type="database",
            config='{"connection_string": "sqlite:///a.db", "query": "SELECT 1"}',
            owner_id="A",
        )
        assert db.list_data_sources("B") == []

    def test_user_b_cannot_get_user_a_source(self, db):
        sid = db.create_data_source(name="A-source", source_type="rss", config="{}", owner_id="A")
        assert db.get_data_source(sid, "B") is None

    def test_user_b_cannot_delete_user_a_source(self, db):
        sid = db.create_data_source(name="A-source", source_type="rss", config="{}", owner_id="A")
        assert db.delete_data_source(sid, "B") is False
        # 数据源仍然存在（未被误删）
        assert db.get_data_source(sid, "A") is not None

    def test_owner_can_list_and_delete_own(self, db):
        sid = db.create_data_source(name="A-source", source_type="rss", config="{}", owner_id="A")
        ids = [s["id"] for s in db.list_data_sources("A")]
        assert sid in ids
        assert db.delete_data_source(sid, "A") is True

    def test_admin_sees_all_and_can_delete(self, db):
        sid = db.create_data_source(name="A-source", source_type="rss", config="{}", owner_id="A")
        ids = [s["id"] for s in db.list_data_sources("admin")]
        assert sid in ids
        assert db.delete_data_source(sid, "admin") is True

    def test_sync_guard_denies_non_owner(self, db):
        """sync 端点用 get_data_source(owner) 做归属校验，非属主视为不存在。"""
        sid = db.create_data_source(
            name="DB-A",
            source_type="database",
            config='{"connection_string": "sqlite:///a.db", "query": "SELECT 1"}',
            owner_id="A",
        )
        assert db.get_data_source(sid, "B") is None
        assert db.get_data_source(sid, "A") is not None


# ── config 脱敏 ──────────────────────────────────────────────────────────


class TestConfigMasking:
    def test_list_masks_connection_string_password(self, db):
        db.create_data_source(
            name="DB",
            source_type="database",
            config='{"connection_string": "mysql://user:supersecret@host:3306/db", "query": "SELECT 1"}',
            owner_id="A",
        )
        sources = db.list_data_sources("A")
        cfg = sources[0]["config"]
        assert "supersecret" not in json.dumps(cfg)
        assert sources[0]["has_secret"] is True
        # 主机信息保留，口令被掩掉
        assert "supersecret" not in cfg.get("connection_string", "")
        assert "mysql" in cfg.get("connection_string", "")

    def test_list_masks_nested_headers_authorization(self, db):
        db.create_data_source(
            name="API",
            source_type="api",
            config='{"url": "https://x", "headers": {"Authorization": "Bearer tok"}}',
            owner_id="A",
        )
        sources = db.list_data_sources("A")
        cfg = sources[0]["config"]
        assert cfg["headers"]["Authorization"] == "***"
        assert sources[0]["has_secret"] is True

    def test_list_masks_password_key(self, db):
        db.create_data_source(
            name="DB",
            source_type="database",
            config='{"password": "hunter2", "host": "db.internal"}',
            owner_id="A",
        )
        sources = db.list_data_sources("A")
        cfg = sources[0]["config"]
        assert "hunter2" not in json.dumps(cfg)
        assert cfg["password"] == "***"
        assert sources[0]["has_secret"] is True

    def test_get_data_source_returns_full_config_for_sync(self, db):
        """sync 路径需要完整（未脱敏）config，get_data_source 不应脱敏。"""
        sid = db.create_data_source(
            name="DB",
            source_type="database",
            config='{"connection_string": "mysql://u:plainpass@h/d", "query": "SELECT 1"}',
            owner_id="A",
        )
        src = db.get_data_source(sid, "A")
        assert "plainpass" in src["config"]

    def test_mask_data_source_config_helper(self):
        cfg, has = mask_data_source_config({"password": "pw", "keep": 1})
        assert cfg["password"] == "***"
        assert cfg["keep"] == 1
        assert has is True

        cfg2, has2 = mask_data_source_config({"url": "https://x", "ok": True})
        assert has2 is False
        assert cfg2["url"] == "https://x"


# ── 只读 SQL 白名单 ──────────────────────────────────────────────────────


class TestReadOnlySQLEnforcement:
    def test_validate_read_only_sql_rejects_writes(self):
        for bad in (
            "DELETE FROM t",
            "DROP TABLE t",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET x = 1",
            "ALTER TABLE t ADD COLUMN c",
            "SELECT 1; DROP TABLE t",
            "WITH x AS (SELECT 1) DELETE FROM t",
            "  -- comment\nDELETE FROM t",
        ):
            with pytest.raises(ValueError):
                validate_read_only_sql(bad)

    def test_validate_read_only_sql_allows_select(self):
        assert validate_read_only_sql("SELECT * FROM t") is None
        assert validate_read_only_sql("  -- comment\nSELECT 1") is None
        # 字符串字面量中的写词不误报
        assert validate_read_only_sql("SELECT 'delete' AS label, 'update' AS l2") is None

    def test_test_connection_does_not_execute_destructive_sql(self, tmp_path):
        """test_connection 只做 SELECT 1 连通性检查，不应执行 DROP。"""
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        src = DBSource(connection_string=f"sqlite:///{db_path}", query="DROP TABLE t")
        assert src.test_connection() is True

        # DROP 未被实际执行 → 表仍然存在
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_fetch_rejects_delete_and_preserves_rows(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        src = DBSource(connection_string=f"sqlite:///{db_path}", query="DELETE FROM t")
        with pytest.raises(ValueError):
            _run(src.fetch())

        conn = sqlite3.connect(db_path)
        cnt = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert cnt == 1

    def test_fetch_rejects_multistatement(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        src = DBSource(connection_string=f"sqlite:///{db_path}", query="SELECT 1; DROP TABLE t")
        with pytest.raises(ValueError):
            _run(src.fetch())

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_fetch_allows_select(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()

        src = DBSource(connection_string=f"sqlite:///{db_path}", query="SELECT id FROM t")
        rows = _run(src.fetch())
        assert rows == [{"id": 42}]


# ── 迁移兼容 ──────────────────────────────────────────────────────────────


class TestMigration:
    def test_old_data_defaults_to_admin(self, tmp_path):
        """模拟旧库（无 owner_id 列）：迁移后回填 admin，admin 可见。"""
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE data_sources (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                type            TEXT    NOT NULL,
                config          TEXT    NOT NULL DEFAULT '{}',
                status          TEXT    NOT NULL DEFAULT 'inactive',
                last_synced_at  REAL,
                created_at      REAL    NOT NULL DEFAULT (strftime('%s','now'))
            )"""
        )
        conn.execute(
            "INSERT INTO data_sources (name, type, config) VALUES ('old', 'rss', '{}')"
        )
        conn.commit()
        conn.close()

        # 打开时触发迁移（加 owner_id + 回填 admin）
        udb = UserDB(path)
        try:
            sources = udb.list_data_sources("admin")
            assert len(sources) == 1
            assert sources[0]["name"] == "old"
            # 旧数据归属 admin → 普通用户看不到
            assert udb.list_data_sources("B") == []
        finally:
            udb.close()

    def test_migration_is_idempotent(self, tmp_path):
        path = str(tmp_path / "a.db")
        udb1 = UserDB(path)
        udb1.create_data_source(name="x", source_type="rss", config="{}", owner_id="A")
        udb1.close()

        # 二次打开（表已含 owner_id）不应报错
        udb2 = UserDB(path)
        try:
            assert len(udb2.list_data_sources("A")) == 1
        finally:
            udb2.close()


# ── API 层（认证关闭环境）────────────────────────────────────────────────


def test_api_list_masks_config_and_cleans_up():
    """GET /sources 返回脱敏 config + has_secret（认证关闭 → owner='admin'）。"""
    from fastapi.testclient import TestClient

    from rag.api import app, user_db

    sid = user_db.create_data_source(
        name="DB",
        source_type="database",
        config='{"connection_string": "mysql://u:supersecret@h:3306/d", "query": "SELECT 1"}',
        owner_id="admin",
    )
    try:
        client = TestClient(app)
        resp = client.get("/sources")
        assert resp.status_code == 200
        src = next(s for s in resp.json()["sources"] if s["id"] == sid)
        assert "supersecret" not in json.dumps(src)
        assert src["has_secret"] is True
        assert src["config"]["connection_string"] != "mysql://u:supersecret@h:3306/d"
    finally:
        user_db.delete_data_source(sid, "admin")
