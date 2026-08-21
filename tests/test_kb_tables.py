"""知识库元数据表测试。"""
import os
import sqlite3
import tempfile


def test_kb_metadata_table_exists():
    """kb_metadata 表应该存在。"""
    from rag.user_db import UserDB
    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = UserDB(db_path)
        db.close()
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kb_metadata'").fetchall()
        assert len(rows) == 1
        conn.close()
    finally:
        os.unlink(db_path)


def test_kb_documents_table_exists():
    """kb_documents 表应该存在。"""
    from rag.user_db import UserDB
    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = UserDB(db_path)
        db.close()
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kb_documents'").fetchall()
        assert len(rows) == 1
        conn.close()
    finally:
        os.unlink(db_path)


def test_kb_documents_has_chunk_count():
    """kb_documents 表应该有 chunk_count 字段。"""
    from rag.user_db import UserDB
    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = UserDB(db_path)
        db.close()
        conn = sqlite3.connect(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(kb_documents)").fetchall()]
        assert "chunk_count" in columns
        conn.close()
    finally:
        os.unlink(db_path)


def test_kb_documents_unique_constraint():
    """同一 kb_id 下 filename 应该唯一。"""
    from rag.user_db import UserDB
    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = UserDB(db_path)
        db.close()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO kb_metadata (kb_id, name) VALUES (?, ?)", ("kb_test", "测试"))
        conn.execute("INSERT INTO kb_documents (kb_id, filename, file_path) VALUES (?, ?, ?)", ("kb_test", "a.txt", "/a.txt"))
        try:
            conn.execute("INSERT INTO kb_documents (kb_id, filename, file_path) VALUES (?, ?, ?)", ("kb_test", "a.txt", "/a.txt"))
            raise AssertionError("应该抛出 IntegrityError")
        except sqlite3.IntegrityError:
            pass
        conn.close()
    finally:
        os.unlink(db_path)
