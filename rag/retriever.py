import logging
import re
import threading

from rank_bm25 import BM25Okapi

from config import settings
from rag.bm25_store import BM25Store
from rag.embedder import embed
from rag.models import Chunk
from rag.vector_store import search as dense_search
from rag.vector_store import search_collection

logger = logging.getLogger(__name__)

# Module-level BM25 cache to avoid rebuilding on every query
_bm25_cache: dict[str, tuple[list[Chunk], BM25Okapi]] = {}
_bm25_cache_lock = threading.Lock()
_bm25_cache_versions: dict[str, int] = {}
_bm25_global_version = 0


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
    global _bm25_global_version
    with _bm25_cache_lock:
        if collection_name:
            _bm25_cache.pop(collection_name, None)
            _bm25_cache_versions[collection_name] = _bm25_cache_versions.get(collection_name, 0) + 1
        else:
            _bm25_cache.clear()
            _bm25_global_version += 1


def _bm25_version(collection_name: str) -> tuple[int, int]:
    return _bm25_global_version, _bm25_cache_versions.get(collection_name, 0)


class Retriever:
    def __init__(self, chunks: list[Chunk], collection_name: str = None):
        self._bm25_version = _bm25_version(collection_name) if collection_name else None
        if not chunks and collection_name:
            # Check module-level cache first
            with _bm25_cache_lock:
                if collection_name in _bm25_cache:
                    cached_chunks, cached_bm25 = _bm25_cache[collection_name]
                    self.chunks = cached_chunks
                    self.collection_name = collection_name
                    self.bm25 = cached_bm25
                    self._bm25_version = _bm25_version(collection_name)
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
                    self._bm25_version = _bm25_version(collection_name)
        else:
            self.bm25 = None

    def _refresh_bm25_if_stale(self) -> None:
        if not self.collection_name:
            return

        while True:
            with _bm25_cache_lock:
                target_version = _bm25_version(self.collection_name)
                if self._bm25_version == target_version:
                    return
                cached = _bm25_cache.get(self.collection_name)
                if cached is not None:
                    self.chunks, self.bm25 = cached
                    self._bm25_version = target_version
                    return

            chunks = _load_all_chunks(self.collection_name)
            bm25 = BM25Okapi([_tokenize(c.text) for c in chunks]) if chunks else None
            with _bm25_cache_lock:
                if _bm25_version(self.collection_name) != target_version:
                    continue
                if bm25 is not None:
                    _bm25_cache[self.collection_name] = (chunks, bm25)
                self.chunks = chunks
                self.bm25 = bm25
                self._bm25_version = target_version
                return

    def retrieve(self, query: str, top_k: int = 5, doc_name: str = None, tags: list[str] = None, weights: dict[str, float] = None) -> list[Chunk]:
        self._refresh_bm25_if_stale()
        dense_hits = []
        try:
            query_vec = embed([query])[0]
            if self.collection_name:
                dense_hits = search_collection(
                    self.collection_name,
                    query_vec,
                    top_k=top_k * 2,
                    doc_name=doc_name,
                    tags=tags,
                )
            else:
                dense_hits = dense_search(
                    query_vec,
                    top_k=top_k * 2,
                    doc_name=doc_name,
                    tags=tags,
                )
        except Exception as exc:
            logger.warning("Embedding/dense 检索失败，降级为纯 BM25: %s", exc)

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
            dense_hits = [c for c in dense_hits if c.doc_name == doc_name]
            sparse_hits = [c for c in sparse_hits if c.doc_name == doc_name]

        return self._rrf_fuse(dense_hits, sparse_hits, top_k, rrf_k=settings.rrf_k, weights=weights)

    @staticmethod
    def _rrf_fuse(dense: list[Chunk], sparse: list[Chunk], top_k: int, rrf_k: int = 60, weights: dict[str, float] = None) -> list[Chunk]:
        import hashlib as _hashlib

        scores: dict[tuple[str, int, str], float] = {}
        representatives: dict[tuple[str, int, str], Chunk] = {}

        def add_score(doc: Chunk, score: float) -> None:
            # BM25 snapshots do not retain doc_permission_id while vector hits do.
            # Fuse by the content identity so the same chunk cannot occupy two ranks.
            key = (doc.doc_name, doc.chunk_index, doc.text)
            scores[key] = scores.get(key, 0) + score
            existing = representatives.get(key)
            if existing is None or (
                existing.doc_permission_id is None and doc.doc_permission_id is not None
            ):
                representatives[key] = doc

        for rank, doc in enumerate(dense):
            base = 1 / (rrf_k + rank + 1)
            if weights:
                h = _hashlib.md5(doc.text.encode()).hexdigest()
                base *= weights(h) if callable(weights) else weights.get(h, 1.0)
            add_score(doc, base)
        for rank, doc in enumerate(sparse):
            base = 1 / (rrf_k + rank + 1)
            if weights:
                h = _hashlib.md5(doc.text.encode()).hexdigest()
                base *= weights(h) if callable(weights) else weights.get(h, 1.0)
            add_score(doc, base)
        return [
            representatives[key]
            for key in sorted(scores, key=lambda key: -scores[key])[:top_k]
        ]
