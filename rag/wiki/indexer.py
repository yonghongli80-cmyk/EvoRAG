"""Wiki Ingestion & Hybrid Indexing Pipeline.

Connects WikiStore and MarkdownASTChunker to Qdrant vector store and SQLite BM25.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from rag.vector_store import (
    _ensure_collection_name,
    add_to_collection,
    delete_doc,
)
from rag.wiki.chunker import MarkdownASTChunker
from rag.wiki.store import WikiStore

logger = logging.getLogger(__name__)


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

    def _get_embed_fn(self) -> Callable[[list[str]], list[list[float]]]:
        if self._embed_fn is not None:
            return self._embed_fn
        from rag.embedder import embed

        return embed

    def sync_page(self, page_id: int, old_title: str | None = None) -> int:
        """Chunk, embed, and index a single Wiki page.

        Returns the number of chunks indexed.
        """
        page = self._store.get_page(page_id)
        if not page:
            return 0

        _ensure_collection_name(page.kb_id)

        if old_title and old_title != page.title:
            delete_doc(page.kb_id, old_title)

        wiki_chunks = self._chunker.chunk_page(page)
        if not wiki_chunks:
            # If page content is empty or resulted in 0 chunks, purge existing points
            delete_doc(page.kb_id, page.title)
            return 0

        std_chunks = [wc.to_chunk() for wc in wiki_chunks]
        embed_fn = self._get_embed_fn()
        embeddings = embed_fn([c.text for c in std_chunks])

        add_to_collection(
            collection_name=page.kb_id,
            chunks=std_chunks,
            embeddings=embeddings,
            tags=list(page.tags),
            document_id=page.id,
            updated_at=page.updated_at,
        )
        return len(std_chunks)

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
            delete_doc(target_kb, target_title)

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
