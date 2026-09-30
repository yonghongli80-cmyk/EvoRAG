"""BM25 倒排索引的 SQLite 持久化。

快照结构分两代：

* **v1** 只存 ``(collection, text, doc_name, chunk_index)``——块没有版本元数据，在版本化
  链路里**不可引用**（密集检索抖动降级纯 BM25 时会让整条请求失败，2026-09-30 实测）；
* **v2** 另存引用与版本过滤所需的字段（``document_id``/``document_version_id``/
  ``version_no``/``validity_status``/``is_current``/``doc_permission_id``）。

v1 快照一律视为**过期**：``has_chunks`` 返回 False，调用方（``Retriever``）于是从向量库
重建并写入 v2——不需要人工迁移步骤，旧行保留但被忽略。``bm25_meta`` 里的
``chunks_schema:<collection>`` 记录该 collection 的快照结构版本。
"""
import sqlite3
import threading

from config import settings

#: 当前快照结构版本；与 ``bm25_meta`` 里记录的值不一致即视为过期。
SNAPSHOT_SCHEMA_VERSION = "2"

#: v2 相对 v1 增加列；``_ensure_table`` 会为既有表补齐（SQLite 支持 ADD COLUMN）。
_VERSION_COLUMNS = (
    ("document_id", "INTEGER"),
    ("document_version_id", "INTEGER"),
    ("version_no", "INTEGER"),
    ("validity_status", "TEXT"),
    ("is_current", "INTEGER"),
    ("doc_permission_id", "INTEGER"),
)

_SCHEMA_KEY_PREFIX = "chunks_schema:"


class BM25Store:
    def __init__(self, db_path: str = None):
        db_path = db_path or settings.bm25_db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._ensure_table()

    def _ensure_table(self):
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS bm25_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                text TEXT NOT NULL,
                doc_name TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                document_id INTEGER,
                document_version_id INTEGER,
                version_no INTEGER,
                validity_status TEXT,
                is_current INTEGER,
                doc_permission_id INTEGER
            )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS bm25_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
            )
            # 迁移既有 v1 表：补列而非重建，旧行保留（随后被判为过期、由调用方重建）。
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(bm25_chunks)")}
            for column, sql_type in _VERSION_COLUMNS:
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE bm25_chunks ADD COLUMN {column} {sql_type}")
            self._conn.commit()

    def _snapshot_is_current(self, collection: str) -> bool:
        """该 collection 的快照是否由当前结构版本写入。"""
        row = self._conn.execute(
            "SELECT value FROM bm25_meta WHERE key = ?", (_SCHEMA_KEY_PREFIX + collection,)
        ).fetchone()
        return row is not None and str(row[0]) == SNAPSHOT_SCHEMA_VERSION

    def save_chunks(self, collection: str, chunks: list):
        """保存 chunks 到 SQLite（先清空该 collection 的旧数据），并标记结构版本。"""
        # Invalidate module-level BM25 cache when underlying data changes
        try:
            from rag.retriever import invalidate_bm25_cache
            invalidate_bm25_cache(collection)
        except ImportError:
            pass
        with self._lock:
            self._conn.execute(
                "DELETE FROM bm25_chunks WHERE collection = ?", (collection,)
            )
            self._conn.executemany(
                "INSERT INTO bm25_chunks (collection, text, doc_name, chunk_index, "
                "document_id, document_version_id, version_no, validity_status, is_current, "
                "doc_permission_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        collection,
                        c.text,
                        c.doc_name,
                        c.chunk_index,
                        getattr(c, "document_id", None),
                        getattr(c, "document_version_id", None),
                        getattr(c, "version_no", None),
                        getattr(c, "validity_status", None),
                        None if getattr(c, "is_current", None) is None else int(c.is_current),
                        getattr(c, "doc_permission_id", None),
                    )
                    for c in chunks
                ],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO bm25_meta (key, value) VALUES (?, ?)",
                (_SCHEMA_KEY_PREFIX + collection, SNAPSHOT_SCHEMA_VERSION),
            )
            self._conn.commit()

    def load_chunks(self, collection: str) -> list:
        """从 SQLite 加载 chunks。结构版本不符时返回空列表，交由调用方重建。"""
        from rag.models import Chunk

        with self._lock:
            if not self._snapshot_is_current(collection):
                return []
            rows = self._conn.execute(
                "SELECT text, doc_name, chunk_index, document_id, document_version_id, "
                "version_no, validity_status, is_current, doc_permission_id "
                "FROM bm25_chunks WHERE collection = ?",
                (collection,),
            ).fetchall()
        return [
            Chunk(
                text=row[0],
                doc_name=row[1],
                chunk_index=row[2],
                document_id=row[3],
                document_version_id=row[4],
                version_no=row[5],
                validity_status=row[6] if row[6] is not None else "valid",
                is_current=True if row[7] is None else bool(row[7]),
                doc_permission_id=row[8],
            )
            for row in rows
        ]

    def has_chunks(self, collection: str) -> bool:
        """该 collection 是否有**当前结构版本**的持久化 chunks。

        结构版本不符（例如 v1 快照没有版本元数据）时返回 False，让调用方从向量库重建：
        拿无版本块冒充证据会让版本化链路失败，宁可重建。
        """
        with self._lock:
            if not self._snapshot_is_current(collection):
                return False
            row = self._conn.execute(
                "SELECT COUNT(*) FROM bm25_chunks WHERE collection = ?",
                (collection,),
            ).fetchone()
        return row[0] > 0

    def invalidate(self, collection: str = None) -> None:
        """Discard stale snapshots and their in-process BM25 indexes."""
        with self._lock:
            if collection is None:
                self._conn.execute("DELETE FROM bm25_chunks")
                self._conn.execute("DELETE FROM bm25_meta")
            else:
                self._conn.execute(
                    "DELETE FROM bm25_chunks WHERE collection = ?", (collection,)
                )
                self._conn.execute(
                    "DELETE FROM bm25_meta WHERE key = ?", (_SCHEMA_KEY_PREFIX + collection,)
                )
            self._conn.commit()
        try:
            from rag.retriever import invalidate_bm25_cache

            invalidate_bm25_cache(collection)
        except ImportError:
            pass

    def close(self):
        self._conn.close()
