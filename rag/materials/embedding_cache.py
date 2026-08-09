"""Separate bounded embedding-cache database.

The cache stores only fingerprint, model key, dimension, and the vector blob —
never ACL, source paths, filenames, questions, or display text. A fingerprint
derives from the normalized unit text plus the parser schema version, so a
parser change invalidates cached embeddings naturally.
"""

from __future__ import annotations

import sqlite3
import struct
import time
from collections.abc import Callable, Sequence
from pathlib import Path

_CLEANUP_TARGET_FRACTION = 0.9


class EmbeddingCache:
    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024 * 1024):
        self._path = path
        self._max_bytes = max_bytes
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            " fingerprint TEXT NOT NULL,"
            " model_key TEXT NOT NULL,"
            " dimension INTEGER NOT NULL,"
            " vector BLOB NOT NULL,"
            " created_at REAL NOT NULL,"
            " last_used_at REAL NOT NULL,"
            " hit_count INTEGER NOT NULL DEFAULT 0,"
            " byte_size INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (fingerprint, model_key, dimension)"
            ")"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_or_embed(
        self,
        fingerprints: Sequence[str],
        texts: Sequence[str],
        model_key: str,
        dimension: int,
        embed_fn: Callable[[list[str]], list[list[float]]],
    ) -> list[list[float]]:
        """Return cached vectors for known fingerprints and embed the rest."""
        results: list[list[float] | None] = [None] * len(fingerprints)
        missing_indexes: list[int] = []
        now = time.time()

        for index, fingerprint in enumerate(fingerprints):
            row = self._conn.execute(
                "SELECT vector, hit_count FROM embeddings "
                "WHERE fingerprint = ? AND model_key = ? AND dimension = ?",
                (fingerprint, model_key, dimension),
            ).fetchone()
            if row is not None:
                results[index] = list(struct.unpack(f"<{len(row['vector']) // 8}d", row["vector"]))
                self._conn.execute(
                    "UPDATE embeddings SET last_used_at = ?, hit_count = hit_count + 1 "
                    "WHERE fingerprint = ? AND model_key = ? AND dimension = ?",
                    (now, fingerprint, model_key, dimension),
                )
            else:
                missing_indexes.append(index)

        if missing_indexes:
            missing_texts = [texts[i] for i in missing_indexes]
            new_vectors = embed_fn(missing_texts)
            for offset, index in enumerate(missing_indexes):
                vector = list(new_vectors[offset])
                blob = struct.pack(f"<{len(vector)}d", *vector)
                self._conn.execute(
                    "INSERT INTO embeddings "
                    "(fingerprint, model_key, dimension, vector, created_at, last_used_at, hit_count, byte_size) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        fingerprints[index],
                        model_key,
                        dimension,
                        sqlite3.Binary(blob),
                        now,
                        now,
                        len(blob),
                    ),
                )
                results[index] = vector
        self._conn.commit()
        self._maybe_cleanup(now)
        return [v for v in results if v is not None]  # type: ignore[misc]

    def _maybe_cleanup(self, now: float) -> None:
        """Low-priority LRU cleanup toward the configured byte cap."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(byte_size), 0) AS total FROM embeddings"
        ).fetchone()
        if row is None or row["total"] <= self._max_bytes:
            return
        target = int(self._max_bytes * _CLEANUP_TARGET_FRACTION)
        over = row["total"] - target
        rows = self._conn.execute(
            "SELECT fingerprint, model_key, dimension, byte_size FROM embeddings "
            "ORDER BY last_used_at ASC"
        ).fetchall()
        freed = 0
        for entry in rows:
            if freed >= over:
                break
            self._conn.execute(
                "DELETE FROM embeddings WHERE fingerprint = ? AND model_key = ? AND dimension = ?",
                (entry["fingerprint"], entry["model_key"], entry["dimension"]),
            )
            freed += entry["byte_size"]
        self._conn.commit()
