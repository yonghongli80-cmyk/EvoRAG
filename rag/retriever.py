import re
import threading

from rank_bm25 import BM25Okapi

from config import settings
from rag.bm25_store import BM25Store
from rag.embedder import embed
from rag.models import Chunk
from rag.vector_store import search as dense_search
from rag.vector_store import search_collection

# Module-level BM25 cache to avoid rebuilding on every query
_bm25_cache: dict[str, tuple[list[Chunk], BM25Okapi]] = {}
_bm25_cache_lock = threading.Lock()


def _tokenize(text: str) -> list[str]:
    """Tokenize text, using jieba for Chinese and whitespace split for others."""
    if re.search(r"[一-鿿]", text):
        return _chinese_tokenize(text)
    return text.split()


def _chinese_tokenize(text: str) -> list[str]:
    try:
        import jieba

        return list(jieba.cut(text))
    except Exception:
        pass
    # Fallback: character-unigram + known bigrams via sliding window
    tokens: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch.strip():
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _load_all_chunks(collection_name: str) -> list[Chunk]:
    """Load all chunks from a Qdrant collection for BM25 indexing.

    NOTE: No read lock is held during the scroll. If concurrent writes occur
    chunks may be added or removed between pages.  This is a known limitation;
    BM25 is rebuilt on each query so any transient inconsistency is ephemeral.
    """
    from rag.vector_store import _get_client

    client = _get_client()
    all_points = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection_name,
            limit=10000,
            offset=offset,
            with_payload=True,
        )
        all_points.extend(points)
        if offset is None:
            break
    chunks = []
    for point in all_points:
        payload = point.payload or {}
        chunks.append(
            Chunk(
                text=payload.get("text", ""),
                doc_name=payload.get("doc_name", ""),
                chunk_index=payload.get("chunk_index", 0),
            )
        )
    return chunks


def invalidate_bm25_cache(collection_name: str = None):
    """Clear BM25 cache for a specific collection or all collections."""
    with _bm25_cache_lock:
        if collection_name:
            _bm25_cache.pop(collection_name, None)
        else:
            _bm25_cache.clear()


class Retriever:
    def __init__(self, chunks: list[Chunk], collection_name: str = None):
        if not chunks and collection_name:
            # Check module-level cache first
            with _bm25_cache_lock:
                if collection_name in _bm25_cache:
                    cached_chunks, cached_bm25 = _bm25_cache[collection_name]
                    self.chunks = cached_chunks
                    self.collection_name = collection_name
                    self.bm25 = cached_bm25
                    return
            store = BM25Store()
            if store.has_chunks(collection_name):
                chunks = store.load_chunks(collection_name)
            else:
                chunks = _load_all_chunks(collection_name)
                store.save_chunks(collection_name, chunks)
            store.close()
        self.chunks = chunks
        self.collection_name = collection_name
        if chunks:
            tokenized = [_tokenize(c.text) for c in chunks]
            self.bm25 = BM25Okapi(tokenized)
            # Cache the BM25 index
            if collection_name:
                with _bm25_cache_lock:
                    _bm25_cache[collection_name] = (self.chunks, self.bm25)
        else:
            self.bm25 = None

    def retrieve(self, query: str, top_k: int = 5, doc_name: str = None, tags: list[str] = None, weights: dict[str, float] = None, allowed_version_ids: set[int] = None, validity_statuses: set[str] = None) -> list[Chunk]:
        query_vec = embed([query])[0]
        if self.collection_name:
            dense_hits = search_collection(
                self.collection_name,
                query_vec,
                top_k=top_k * 2,
                doc_name=doc_name,
                tags=tags,
                allowed_version_ids=allowed_version_ids,
                validity_statuses=validity_statuses,
            )
        else:
            dense_hits = dense_search(query_vec, top_k=top_k * 2)

        sparse_hits = []
        if self.bm25 is not None and self.chunks:
            tokenized_q = _tokenize(query)
            bm25_scores = self.bm25.get_scores(tokenized_q)
            bm25_indices = sorted(
                range(len(bm25_scores)),
                key=lambda i: bm25_scores[i],
                reverse=True,
            )[: top_k * 2]
            sparse_hits = [self.chunks[i] for i in bm25_indices]

        # 如果指定了文档名，过滤 BM25 结果
        if doc_name:
            sparse_hits = [c for c in sparse_hits if c.doc_name == doc_name]

        # 版本过滤：BM25 缓存若缺少版本元数据则整体禁用，避免泄漏历史内容
        if allowed_version_ids is not None:
            if self.chunks and getattr(self.chunks[0], "document_version_id", None) is not None:
                sparse_hits = [c for c in sparse_hits if c.document_version_id in allowed_version_ids]
            else:
                sparse_hits = []
        if validity_statuses:
            if self.chunks and getattr(self.chunks[0], "validity_status", None) is not None:
                sparse_hits = [c for c in sparse_hits if c.validity_status in validity_statuses]
            else:
                sparse_hits = []

        return self._rrf_fuse(dense_hits, sparse_hits, top_k, rrf_k=settings.rrf_k, weights=weights)

    @staticmethod
    def _rrf_fuse(dense: list[Chunk], sparse: list[Chunk], top_k: int, rrf_k: int = 60, weights: dict[str, float] = None) -> list[Chunk]:
        import hashlib as _hashlib

        scores: dict[Chunk, float] = {}
        for rank, doc in enumerate(dense):
            base = 1 / (rrf_k + rank + 1)
            if weights:
                h = _hashlib.md5(doc.text.encode()).hexdigest()
                base *= weights.get(h, 1.0)
            scores[doc] = scores.get(doc, 0) + base
        for rank, doc in enumerate(sparse):
            base = 1 / (rrf_k + rank + 1)
            if weights:
                h = _hashlib.md5(doc.text.encode()).hexdigest()
                base *= weights.get(h, 1.0)
            scores[doc] = scores.get(doc, 0) + base
        return sorted(scores, key=lambda d: -scores[d])[:top_k]
