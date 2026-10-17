"""SQLite persistence store for Hypertext Wiki pages, revisions, and link graph."""

from __future__ import annotations

import contextlib
import difflib
import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from rag.wiki.models import WikiPage, WikiRevision, compute_content_sha256, extract_wiki_links


class WikiStore:
    """Thread-safe SQLite store for Wiki pages, versioned snapshots, and bidirectional links."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            from config import settings

            db_path = settings.wiki_db_path
        self._path = Path(db_path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA busy_timeout = 30000;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    current_revision_id INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(kb_id, slug)
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb_slug ON wiki_pages(kb_id, slug);
                CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb_title ON wiki_pages(kb_id, title);

                CREATE TABLE IF NOT EXISTS wiki_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
                    revision_num INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    delta TEXT,
                    author_id TEXT,
                    comment TEXT NOT NULL,
                    sha256_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(page_id, revision_num)
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page ON wiki_revisions(page_id, revision_num);

                CREATE TABLE IF NOT EXISTS wiki_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
                    target_title TEXT NOT NULL,
                    target_page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_links_source ON wiki_links(source_page_id);
                CREATE INDEX IF NOT EXISTS idx_wiki_links_target_title ON wiki_links(target_title);
                CREATE INDEX IF NOT EXISTS idx_wiki_links_target_page ON wiki_links(target_page_id);
                """
            )
            self._conn.commit()

    @staticmethod
    def _row_to_page(row: sqlite3.Row) -> WikiPage:
        raw_tags = row["tags"]
        try:
            tags = tuple(json.loads(raw_tags)) if raw_tags else ()
        except (json.JSONDecodeError, TypeError):
            tags = ()
        return WikiPage(
            id=int(row["id"]),
            kb_id=str(row["kb_id"]),
            title=str(row["title"]),
            slug=str(row["slug"]),
            content=str(row["content"]),
            tags=tags,
            current_revision_id=int(row["current_revision_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> WikiRevision:
        return WikiRevision(
            id=int(row["id"]),
            page_id=int(row["page_id"]),
            revision_num=int(row["revision_num"]),
            title=str(row["title"]),
            content=str(row["content"]),
            delta=str(row["delta"]) if row["delta"] is not None else None,
            author_id=str(row["author_id"]) if row["author_id"] is not None else None,
            comment=str(row["comment"]),
            sha256_hash=str(row["sha256_hash"]),
            created_at=float(row["created_at"]),
        )

    def _sync_outgoing_links(self, source_page_id: int, kb_id: str, content: str) -> None:
        """Parse internal [[Target]] links and update wiki_links records."""
        titles = extract_wiki_links(content)
        self._conn.execute("DELETE FROM wiki_links WHERE source_page_id = ?", (source_page_id,))
        now = time.time()
        for title in titles:
            target_row = self._conn.execute(
                "SELECT id FROM wiki_pages WHERE kb_id = ? AND title = ?", (kb_id, title)
            ).fetchone()
            target_id = int(target_row["id"]) if target_row else None
            self._conn.execute(
                "INSERT INTO wiki_links (source_page_id, target_title, target_page_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (source_page_id, title, target_id, now),
            )

    def _resolve_inbound_links(self, page_id: int, kb_id: str, title: str) -> None:
        """Resolve previously dangling links pointing to this newly created/renamed title."""
        self._conn.execute(
            """
            UPDATE wiki_links
            SET target_page_id = ?
            WHERE target_title = ?
              AND target_page_id IS NULL
              AND source_page_id IN (SELECT id FROM wiki_pages WHERE kb_id = ?)
            """,
            (page_id, title, kb_id),
        )

    def create_page(
        self,
        kb_id: str,
        title: str,
        content: str,
        *,
        slug: str | None = None,
        tags: Sequence[str] = (),
        author_id: str | None = None,
        comment: str = "Initial creation",
    ) -> WikiPage:
        title = title.strip()
        if not title:
            raise ValueError("title cannot be empty")
        slug = (slug.strip() if slug else title)
        now = time.time()
        tags_json = json.dumps(list(tags), ensure_ascii=False)

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)
            ).fetchone()
            if existing:
                raise ValueError(f"Page with slug '{slug}' already exists in kb '{kb_id}' (Unique constraint)")

            existing_title = self._conn.execute(
                "SELECT id FROM wiki_pages WHERE kb_id = ? AND title = ?", (kb_id, title)
            ).fetchone()
            if existing_title:
                raise ValueError(f"Page with title '{title}' already exists in kb '{kb_id}' (Unique constraint)")

            cursor = self._conn.execute(
                """
                INSERT INTO wiki_pages (kb_id, title, slug, content, tags, current_revision_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (kb_id, title, slug, content, tags_json, now, now),
            )
            page_id = int(cursor.lastrowid)

            sha_hash = compute_content_sha256(title, content)
            rev_cursor = self._conn.execute(
                """
                INSERT INTO wiki_revisions (page_id, revision_num, title, content, delta, author_id, comment, sha256_hash, created_at)
                VALUES (?, 1, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (page_id, title, content, author_id, comment, sha_hash, now),
            )
            rev_id = int(rev_cursor.lastrowid)

            self._conn.execute(
                "UPDATE wiki_pages SET current_revision_id = ? WHERE id = ?",
                (rev_id, page_id),
            )

            self._sync_outgoing_links(page_id, kb_id, content)
            self._resolve_inbound_links(page_id, kb_id, title)
            self._conn.commit()

            return self.get_page(page_id)  # type: ignore[return-value]

    def update_page(
        self,
        page_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: Sequence[str] | None = None,
        author_id: str | None = None,
        comment: str = "Update",
    ) -> WikiPage:
        now = time.time()
        with self._lock:
            cur_row = self._conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
            if not cur_row:
                raise ValueError(f"WikiPage with id {page_id} not found")

            old_title = str(cur_row["title"])
            old_content = str(cur_row["content"])
            new_title = title.strip() if title is not None else old_title
            new_content = content if content is not None else old_content

            if not new_title:
                raise ValueError("title cannot be empty")

            if new_title != old_title:
                existing_title = self._conn.execute(
                    "SELECT id FROM wiki_pages WHERE kb_id = ? AND title = ? AND id != ?",
                    (str(cur_row["kb_id"]), new_title, page_id),
                ).fetchone()
                if existing_title:
                    raise ValueError(f"Page with title '{new_title}' already exists in kb '{cur_row['kb_id']}' (Unique constraint)")

            tags_json = json.dumps(list(tags), ensure_ascii=False) if tags is not None else cur_row["tags"]

            # Compute delta
            diff = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"rev/{old_title}",
                tofile=f"rev/{new_title}",
            )
            delta = "".join(diff) or None

            max_rev_row = self._conn.execute(
                "SELECT COALESCE(MAX(revision_num), 0) AS max_rev FROM wiki_revisions WHERE page_id = ?",
                (page_id,),
            ).fetchone()
            new_rev_num = int(max_rev_row["max_rev"]) + 1

            sha_hash = compute_content_sha256(new_title, new_content)
            rev_cursor = self._conn.execute(
                """
                INSERT INTO wiki_revisions (page_id, revision_num, title, content, delta, author_id, comment, sha256_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (page_id, new_rev_num, new_title, new_content, delta, author_id, comment, sha_hash, now),
            )
            rev_id = int(rev_cursor.lastrowid)

            self._conn.execute(
                """
                UPDATE wiki_pages
                SET title = ?, content = ?, tags = ?, current_revision_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_title, new_content, tags_json, rev_id, now, page_id),
            )

            kb_id = str(cur_row["kb_id"])
            self._sync_outgoing_links(page_id, kb_id, new_content)
            if new_title != old_title:
                self._resolve_inbound_links(page_id, kb_id, new_title)
            self._conn.commit()

            return self.get_page(page_id)  # type: ignore[return-value]

    def rollback_revision(
        self,
        page_id: int,
        revision_num: int,
        *,
        author_id: str | None = None,
    ) -> WikiPage:
        with self._lock:
            target_rev = self._conn.execute(
                "SELECT * FROM wiki_revisions WHERE page_id = ? AND revision_num = ?",
                (page_id, revision_num),
            ).fetchone()
            if not target_rev:
                raise ValueError(f"Revision {revision_num} for page {page_id} not found")

            comment = f"回滚至版本 #{revision_num}"
            return self.update_page(
                page_id,
                title=str(target_rev["title"]),
                content=str(target_rev["content"]),
                author_id=author_id,
                comment=comment,
            )

    def get_page(self, page_id: int) -> WikiPage | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
            return self._row_to_page(row) if row else None

    def get_page_by_slug(self, kb_id: str, slug: str) -> WikiPage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wiki_pages WHERE kb_id = ? AND slug = ?", (kb_id, slug)
            ).fetchone()
            return self._row_to_page(row) if row else None

    def get_page_by_title(self, kb_id: str, title: str) -> WikiPage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wiki_pages WHERE kb_id = ? AND title = ?", (kb_id, title)
            ).fetchone()
            return self._row_to_page(row) if row else None

    def list_pages(
        self,
        kb_id: str | None = None,
        *,
        tag: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WikiPage]:
        query = "SELECT * FROM wiki_pages"
        params: list[object] = []
        conditions: list[str] = []

        if kb_id is not None:
            conditions.append("kb_id = ?")
            params.append(kb_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            pages = [self._row_to_page(r) for r in rows]

        if tag is not None:
            pages = [p for p in pages if tag in p.tags]
        return pages

    def delete_page(self, page_id: int) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM wiki_revisions WHERE page_id = ?", (page_id,))
            self._conn.execute("DELETE FROM wiki_links WHERE source_page_id = ?", (page_id,))
            self._conn.execute("UPDATE wiki_links SET target_page_id = NULL WHERE target_page_id = ?", (page_id,))
            cursor = self._conn.execute("DELETE FROM wiki_pages WHERE id = ?", (page_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def get_revisions(self, page_id: int) -> list[WikiRevision]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wiki_revisions WHERE page_id = ? ORDER BY revision_num ASC",
                (page_id,),
            ).fetchall()
            return [self._row_to_revision(r) for r in rows]

    def get_revision_by_num(self, page_id: int, revision_num: int) -> WikiRevision | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wiki_revisions WHERE page_id = ? AND revision_num = ?",
                (page_id, revision_num),
            ).fetchone()
            return self._row_to_revision(row) if row else None

    def get_revision(self, revision_id: int) -> WikiRevision | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wiki_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            return self._row_to_revision(row) if row else None

    def get_outgoing_links(self, page_id: int) -> list[dict]:
        with self._lock:
            page = self.get_page(page_id)
            if not page:
                return []
            rows = self._conn.execute(
                """
                SELECT l.target_title,
                       COALESCE(l.target_page_id, p.id) AS resolved_page_id
                FROM wiki_links l
                LEFT JOIN wiki_pages p ON p.kb_id = ? AND p.title = l.target_title
                WHERE l.source_page_id = ?
                """,
                (page.kb_id, page_id),
            ).fetchall()
            return [
                {
                    "target_title": str(r["target_title"]),
                    "target_page_id": int(r["resolved_page_id"]) if r["resolved_page_id"] is not None else None,
                }
                for r in rows
            ]

    def get_backlinks(self, page_id: int) -> list[WikiPage]:
        """Find all pages that link to page_id (either by resolved id or matching target title)."""
        with self._lock:
            target = self.get_page(page_id)
            if not target:
                return []
            rows = self._conn.execute(
                """
                SELECT DISTINCT p.*
                FROM wiki_pages p
                JOIN wiki_links l ON l.source_page_id = p.id
                WHERE (l.target_page_id = ? OR (l.target_title = ? AND p.kb_id = ?))
                  AND p.id != ?
                ORDER BY p.updated_at DESC
                """,
                (page_id, target.title, target.kb_id, page_id),
            ).fetchall()
            return [self._row_to_page(r) for r in rows]

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._conn.close()


_default_wiki_store: WikiStore | None = None
_store_lock = threading.Lock()


def get_wiki_store(db_path: str | Path | None = None) -> WikiStore:
    """Return the global default WikiStore singleton or a custom instance."""
    global _default_wiki_store
    if db_path is not None:
        return WikiStore(db_path)
    if _default_wiki_store is None:
        with _store_lock:
            if _default_wiki_store is None:
                _default_wiki_store = WikiStore()
    return _default_wiki_store


def reset_wiki_store() -> None:
    """Close and reset the global default WikiStore singleton."""
    global _default_wiki_store
    with _store_lock:
        if _default_wiki_store is not None:
            _default_wiki_store.close()
            _default_wiki_store = None


