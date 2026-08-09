"""BM25 倒排索引的 SQLite 持久化。"""
import sqlite3
import threading

from config import settings

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
                chunk_index INTEGER NOT NULL
            )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS bm25_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
            )
            self._conn.commit()

    def save_chunks(self, collection: str, chunks: list):
        """保存 chunks 到 SQLite（先清空该 collection 的旧数据）。"""
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
                "INSERT INTO bm25_chunks (collection, text, doc_name, chunk_index) VALUES (?, ?, ?, ?)",
                [(collection, c.text, c.doc_name, c.chunk_index) for c in chunks],
            )
            self._conn.commit()

    def load_chunks(self, collection: str) -> list:
        """从 SQLite 加载 chunks。返回 Chunk 列表，无数据返回空列表。"""
        from rag.models import Chunk

        with self._lock:
            rows = self._conn.execute(
                "SELECT text, doc_name, chunk_index FROM bm25_chunks WHERE collection = ?",
                (collection,),
            ).fetchall()
        return [Chunk(text=r[0], doc_name=r[1], chunk_index=r[2]) for r in rows]

    def has_chunks(self, collection: str) -> bool:
        """检查该 collection 是否有持久化的 chunks。"""
        with self._lock:
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
            else:
                self._conn.execute(
                    "DELETE FROM bm25_chunks WHERE collection = ?", (collection,)
                )
            self._conn.commit()
        try:
            from rag.retriever import invalidate_bm25_cache

            invalidate_bm25_cache(collection)
        except ImportError:
            pass

    def close(self):
        self._conn.close()
