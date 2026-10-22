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
    delete_doc,
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

        _ensure_collection_name(page.kb_id)

        stale_titles = self._stale_titles(page, old_title)
        wiki_chunks = self._chunker.chunk_page(page)
        std_chunks = [
            wc.to_chunk(
                document_version_id=page.current_revision_id,
            )
            for wc in wiki_chunks
        ]
        if not std_chunks:
            # An emptied page has nothing to add first; its old points must go.
            for title in stale_titles:
                delete_doc(page.kb_id, title, chunk_kind="wiki")
            return 0

        # Embed before touching the index: an embedding outage must not leave the
        # page with no points at all.
        embed_fn = self._get_embed_fn()
        embeddings = embed_fn([c.text for c in std_chunks])
        if len(embeddings) != len(std_chunks):
            raise ValueError("wiki embedding count does not match chunk count")

        stale_point_ids = self._collect_point_ids(page.kb_id, stale_titles)
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

    def _collect_point_ids(self, kb_id: str, titles: list[str]) -> list[str]:
        """Point ids of the wiki chunks currently indexed under ``titles``."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from rag.vector_store import _get_client

        client = _get_client()
        if not client.collection_exists(kb_id):
            return []
        point_ids: list[str] = []
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
                    with_payload=False,
                )
                point_ids.extend(str(record.id) for record in records)
                if offset is None:
                    break
        return point_ids

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
        target_kb = kb_id
        target_title = title

        if target_kb is None or target_title is None:
            page = self._store.get_page(page_id)
            if page is not None:
                target_kb = target_kb or page.kb_id
                target_title = target_title or page.title

        if target_kb and target_title:
            delete_doc(target_kb, target_title, chunk_kind="wiki")

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
                "(document_id, version_id, content_hash, job_type, status, "
                " available_at, created_at, updated_at) "
                "SELECT ?, 0, ?, ?, 'queued', ?, ?, ? "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM document_index_jobs WHERE document_id = ? "
                "  AND job_type = ? AND status IN ('queued','retry','running'))",
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
