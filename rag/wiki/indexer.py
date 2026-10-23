"""Wiki Ingestion & Hybrid Indexing Pipeline.

Connects WikiStore and MarkdownASTChunker to Qdrant vector store and SQLite BM25.

Index writes are ordered so a failure can never make a page permanently
unretrievable: the new revision is embedded and upserted *before* the previous
revision's points are removed (point-id scoped, so a partial write leaves the
old chunks searchable). Failures are reported to the caller, which records a
``wiki_reindex`` job for the shared worker to retry.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from rag.vector_store import (
    _ensure_collection_name,
    add_to_collection,
)
from rag.wiki.chunker import MarkdownASTChunker
from rag.wiki.models import WikiPage
from rag.wiki.store import WikiStore

if TYPE_CHECKING:
    from rag.user_db import UserDB

logger = logging.getLogger(__name__)

#: Job type drained by the material worker for pages whose index write failed.
WIKI_REINDEX_JOB_TYPE = "wiki_reindex"

_SCROLL_PAGE = 256


class WikiIndexer:
    """Synchronizes Wiki pages with dense vector and sparse lexical indices."""

    def __init__(
        self,
        wiki_store: WikiStore,
        chunker: MarkdownASTChunker | None = None,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        self._store = wiki_store
        self._chunker = chunker or MarkdownASTChunker()
        self._embed_fn = embed_fn

    @property
    def store(self) -> WikiStore:
        """The store this indexer is bound to.

        Public so callers can tell whether a cached indexer still points at the current store
        (the API layer rebuilds it when the wiki singleton is reset) without reaching into
        ``_store``.
        """
        return self._store

    def _get_embed_fn(self) -> Callable[[list[str]], list[list[float]]]:
        if self._embed_fn is not None:
            return self._embed_fn
        from rag.embedder import embed

        return embed

    def sync_page(self, page_id: int, old_title: str | None = None) -> int:
        """Chunk, embed, and index a single Wiki page.

        Writes the new revision before removing the old one, so an embedding or
        vector-store failure leaves the previous revision searchable instead of
        deleting the page from the index. Returns the number of chunks indexed.
        """
        page = self._store.get_page(page_id)
        if not page:
            return 0

        stale_titles = self._stale_titles(page, old_title)
        wiki_chunks = self._chunker.chunk_page(page)
        std_chunks = [
            wc.to_chunk(
                document_version_id=page.current_revision_id,
            )
            for wc in wiki_chunks
        ]
        # Embed before opening the writer transaction. A slow/outdated embed must
        # not block edits; the revision is checked again under SQLite's writer lock.
        embeddings: list[list[float]] = []
        if std_chunks:
            embed_fn = self._get_embed_fn()
            embeddings = embed_fn([c.text for c in std_chunks])
            if len(embeddings) != len(std_chunks):
                raise ValueError("wiki embedding count does not match chunk count")

        with self._store.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT kb_id, current_revision_id FROM wiki_pages WHERE id = ?", (page_id,)
            ).fetchone()
            if (
                current is None
                or str(current["kb_id"]) != page.kb_id
                or int(current["current_revision_id"]) != page.current_revision_id
            ):
                return 0

            stale_titles.extend(
                str(row[0])
                for row in conn.execute(
                    "SELECT title FROM wiki_revisions WHERE page_id = ?", (page_id,)
                )
            )
            stale_point_ids = self._collect_point_ids(page.kb_id, page.id, stale_titles, conn)
            if std_chunks:
                _ensure_collection_name(page.kb_id)
                add_to_collection(
                    collection_name=page.kb_id,
                    chunks=std_chunks,
                    embeddings=embeddings,
                    tags=list(page.tags),
                    document_id=page.id,
                    document_version_id=page.current_revision_id,
                    updated_at=page.updated_at,
                )
            self._delete_point_ids(page.kb_id, stale_point_ids)
        return len(std_chunks)

    def _stale_titles(self, page: WikiPage, old_title: str | None) -> list[str]:
        """Every title this page has been indexed under (including former names)."""
        titles = {page.title}
        if old_title:
            titles.add(old_title)
        try:
            for revision in self._store.get_revisions(page.id):
                titles.add(revision.title)
        except Exception:
            logger.debug("could not read wiki revisions for page %s", page.id, exc_info=True)
        titles.discard("")
        return sorted(titles)

    def _collect_point_ids(self, kb_id: str, page_id: int, titles: list[str], conn) -> list[str]:
        """Collect this page's points and safe legacy title-only points."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from rag.vector_store import _get_client

        client = _get_client()
        if not client.collection_exists(kb_id):
            return []
        point_ids: set[str] = set()
        page_id_filter = Filter(
            must=[
                FieldCondition(key="document_id", match=MatchValue(value=page_id)),
                FieldCondition(key="chunk_kind", match=MatchValue(value="wiki")),
            ]
        )
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=kb_id,
                limit=_SCROLL_PAGE,
                offset=offset,
                scroll_filter=page_id_filter,
                with_payload=True,
            )
            point_ids.update(str(record.id) for record in records)
            if offset is None:
                break
        for title in titles:
            offset = None
            while True:
                records, offset = client.scroll(
                    collection_name=kb_id,
                    limit=_SCROLL_PAGE,
                    offset=offset,
                    scroll_filter=Filter(
                        must=[
                            FieldCondition(key="doc_name", match=MatchValue(value=title)),
                            FieldCondition(key="chunk_kind", match=MatchValue(value="wiki")),
                        ]
                    ),
                    with_payload=True,
                )
                owner = conn.execute(
                    "SELECT id FROM wiki_pages WHERE kb_id = ? AND title = ? AND id != ? LIMIT 1",
                    (kb_id, title, page_id),
                ).fetchone()
                for record in records:
                    payload = record.payload or {}
                    point_owner = payload.get("document_id")
                    if point_owner is not None:
                        if str(point_owner) == str(page_id):
                            point_ids.add(str(record.id))
                    elif owner is None:
                        point_ids.add(str(record.id))
                if offset is None:
                    break
        return sorted(point_ids)

    def _delete_point_ids(self, kb_id: str, point_ids: list[str]) -> int:
        """Delete exactly the collected ids so freshly written points survive."""
        if not point_ids:
            return 0
        from qdrant_client.models import PointIdsList

        from rag.vector_store import _get_client, _invalidate_bm25, _write_lock

        with _write_lock.write():
            client = _get_client()
            if not client.collection_exists(kb_id):
                return 0
            client.delete(
                collection_name=kb_id,
                points_selector=PointIdsList(points=list(point_ids)),
            )
        _invalidate_bm25(kb_id)
        return len(point_ids)

    def delete_page(
        self,
        page_id: int,
        kb_id: str | None = None,
        title: str | None = None,
    ) -> None:
        """Remove indexed points for a Wiki page across dense and sparse stores."""
        with self._store.transaction(immediate=True) as conn:
            row = conn.execute("SELECT kb_id, title FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
            target_kb = kb_id or (str(row["kb_id"]) if row else None)
            target_title = title or (str(row["title"]) if row else None)
            if target_kb:
                titles = [target_title] if target_title else []
                if row:
                    titles.extend(str(r[0]) for r in conn.execute(
                        "SELECT title FROM wiki_revisions WHERE page_id = ?", (page_id,)
                    ))
                ids = self._collect_point_ids(target_kb, page_id, titles, conn)
                self._delete_point_ids(target_kb, ids)

    def delete_page_and_index(self, page_id: int) -> bool:
        """Delete vectors and the page atomically with respect to other indexers."""
        with self._store.transaction(immediate=True) as conn:
            row = conn.execute("SELECT kb_id, title FROM wiki_pages WHERE id = ?", (page_id,)).fetchone()
            if row is None:
                return False
            titles = [str(row["title"])]
            titles.extend(str(r[0]) for r in conn.execute(
                "SELECT title FROM wiki_revisions WHERE page_id = ?", (page_id,)
            ))
            ids = self._collect_point_ids(str(row["kb_id"]), page_id, titles, conn)
            self._delete_point_ids(str(row["kb_id"]), ids)
            return self._store.delete_page(page_id)

    def reindex_all(self, kb_id: str) -> dict[str, int]:
        """Reindex all pages in a knowledge base."""
        pages = self._store.list_pages(kb_id=kb_id, limit=10000)
        chunks_indexed = 0
        pages_synced = 0

        for page in pages:
            synced = self.sync_page(page.id)
            chunks_indexed += synced
            pages_synced += 1

        return {
            "pages_synced": pages_synced,
            "chunks_indexed": chunks_indexed,
        }


def enqueue_wiki_reindex_job(
    db: UserDB,
    page_id: int,
    revision_id: int,
    now: float | None = None,
) -> bool:
    """Queue one bounded reindex attempt for a page whose index write failed.

    The job lives in ``document_index_jobs`` so the existing ``MaterialWorker``
    lease/retry machinery performs the retry; no second scheduler is created.
    ``version_id`` is ``0`` because a Wiki page is not a material version. The
    insert is idempotent: an already active retry for the page suppresses a
    duplicate. Returns ``True`` when a job row was created.
    """
    now = now if now is not None else time.time()
    try:
        with db._lock, db.transaction(immediate=True):
            cursor = db._conn.execute(
                "INSERT INTO document_index_jobs "
                "(document_id, version_id, content_hash, job_type, job_scope, status, "
                " available_at, created_at, updated_at) "
                "SELECT ?, 0, ?, ?, 'wiki', 'queued', ?, ?, ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM document_index_jobs WHERE document_id = ? "
                "  AND job_scope = 'wiki' AND job_type = ? "
                "  AND status IN ('queued','retry','running'))",
                (
                    page_id,
                    str(revision_id),
                    WIKI_REINDEX_JOB_TYPE,
                    now,
                    now,
                    now,
                    page_id,
                    WIKI_REINDEX_JOB_TYPE,
                ),
            )
            return cursor.rowcount == 1
    except Exception:
        logger.exception("could not enqueue wiki reindex job for page %s", page_id)
        return False
