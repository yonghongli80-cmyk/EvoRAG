import json
import logging
import re
import threading
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace

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

#: ``Chunk.metadata`` key carrying the Qdrant ``tags`` payload (A-04).
#:
#: The vector store writes tags as a top-level payload list, while ``Chunk.metadata`` is
#: text-only: chunks rebuilt from a payload or from the BM25 snapshot keep the list here as
#: its JSON encoding, so the sparse path can apply the same tag filter as the dense path.
TAGS_METADATA_KEY = "tags"


def encode_tags(tags) -> str:
    """Encode a tag list for ``Chunk.metadata`` (JSON, so tags may contain commas)."""
    return json.dumps([str(tag) for tag in tags], ensure_ascii=False)


def decode_tags(chunk: Chunk) -> tuple[str, ...]:
    """The tags of a chunk; ``()`` when it carries no tag data (older snapshots)."""
    metadata = getattr(chunk, "metadata", None)
    if not isinstance(metadata, Mapping):
        return ()
    raw = metadata.get(TAGS_METADATA_KEY)
    if not raw:
        return ()
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(tag) for tag in raw if str(tag))
    text = str(raw).strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return tuple(part.strip() for part in text.split(",") if part.strip())
    if isinstance(parsed, list):
        return tuple(str(tag) for tag in parsed if str(tag))
    return (text,)


def _metadata_with_payload_tags(payload: dict) -> dict:
    """Chunk metadata rebuilt from a Qdrant payload, keeping the ``tags`` payload list."""
    metadata = dict(payload.get("metadata") or {})
    payload_tags = payload.get("tags")
    if payload_tags and TAGS_METADATA_KEY not in metadata:
        metadata[TAGS_METADATA_KEY] = encode_tags(payload_tags)
    return metadata


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
    the snapshot is versioned/invalidated on every write, so a transient
    inconsistency is corrected by the next rebuild.
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
                doc_permission_id=payload.get("doc_permission_id"),
                # Tags live in the payload's own list; they must survive the rebuild or the
                # sparse path silently ignores tag filters (A-04).
                metadata=_metadata_with_payload_tags(payload),
                document_id=payload.get("document_id"),
                document_version_id=payload.get("document_version_id"),
                version_no=payload.get("version_no"),
                is_current=bool(payload.get("is_current", True)),
                validity_status=payload.get("validity_status", "valid"),
                section_title=payload.get("section_title"),
                updated_at=payload.get("updated_at"),
                # ``chunk_kind`` decides the ACL branch (wiki pages are keyed by KB view, not by
                # per-document permission) — dropping it made the BM25 path mis-delete wiki
                # chunks (A-04).
                chunk_kind=payload.get("chunk_kind"),
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


#: One build lock per collection: the cold build is the expensive step (full scroll + tokenise),
#: and two concurrent first requests must not both pay it. Kept separate from ``_bm25_cache_lock``
#: so a long build never blocks cache reads of other collections.
_collection_build_locks: dict[str, threading.Lock] = {}
_collection_build_locks_lock = threading.Lock()


def _collection_build_lock(collection_name: str | None):
    """The build lock for one collection; an in-memory retriever (no collection) needs none."""
    if not collection_name:
        return nullcontext()
    with _collection_build_locks_lock:
        return _collection_build_locks.setdefault(collection_name, threading.Lock())


def _in_version_scope(
    chunk: Chunk,
    document_versions: set[int] | None,
    wiki_revisions: set[int] | None,
) -> bool:
    """Does this chunk fall inside the caller's version scope for **its own** id space?

    ``document_version_id`` means different things per domain (S4 review, 2026-10-22): for a
    wiki chunk it is a revision id, for every other chunk it is a ``document_versions.id``.
    The two id spaces can collide, so a single merged set let a visible wiki revision unlock a
    colliding document version. Each domain is therefore matched only against its own scope.

    Fail-closed rule: when a version filter is active but the caller did not supply a wiki
    scope, wiki chunks are excluded. A caller that never computed wiki visibility (the
    comparison tool, an ad-hoc version filter) must not inherit wiki access from an id
    collision; administrators pass ``None`` for both scopes and stay unrestricted.
    """
    is_wiki = getattr(chunk, "chunk_kind", None) == "wiki"
    if is_wiki:
        if wiki_revisions is not None:
            return chunk.document_version_id in wiki_revisions
        return document_versions is None
    if document_versions is not None:
        return chunk.document_version_id in document_versions
    return True


def _version_scope_filter(
    document_versions: set[int] | None, wiki_revisions: set[int] | None
) -> set[int] | None:
    """The union to push into Qdrant, or ``None`` to keep the query unfiltered.

    ``document_version_id`` is the single payload field both domains store their version in, so
    a filtered query needs the union of the two scopes. When only one domain is restricted the
    filter is still the known ids (cross-domain hits are then dropped by
    :func:`_in_version_scope`); when documents are unrestricted the query must stay unfiltered,
    or the wiki scope alone would silently exclude every document.
    """
    if document_versions is None and wiki_revisions is None:
        return None
    if document_versions is None:
        return None
    merged = set(document_versions)
    if wiki_revisions:
        merged |= set(wiki_revisions)
    return merged


class Retriever:
    """Dense + sparse retrieval over one knowledge base collection.

    Construction is cheap by design (A-08): the BM25 index — a full ``client.scroll`` of the
    collection plus tokenisation — is built on **first retrieval**, not in ``__init__``.
    ``_get_or_create_pipeline`` runs while the caller holds the knowledge-base write lock, so
    building the index there froze every knowledge base's reads and writes for the whole cold
    start. ``_ensure_bm25`` builds it once under double-checked locking, so concurrent first
    requests share one build.
    """

    def __init__(self, chunks: list[Chunk] | None = None, collection_name: str = None):
        chunks = list(chunks or [])
        self.collection_name = collection_name
        self.chunks: list[Chunk] = chunks
        self.bm25: BM25Okapi | None = None
        self._bm25_version = _bm25_version(collection_name) if collection_name else None
        self._bm25_ready = False
        self._init_lock = threading.Lock()
        if not chunks and collection_name:
            # 缓存命中只是内存读：命中就立即复用，不必等到第一次检索。
            with _bm25_cache_lock:
                cached = _bm25_cache.get(collection_name)
                if cached is not None:
                    self.chunks, self.bm25 = cached
                    self._bm25_version = _bm25_version(collection_name)
                    self._bm25_ready = True

    def _ensure_bm25(self) -> None:
        """Build the sparse index on first use; double-checked so it is built only once.

        Two levels of double-checking (A-08):

        * the instance flag + ``_init_lock`` keep one retriever from building twice;
        * a per-collection build lock plus a second cache read keep *different* retrievers
          serving the same collection from scrolling and tokenising it concurrently.
        """
        if self._bm25_ready:
            return
        with self._init_lock:
            if self._bm25_ready:
                return
            build_lock = _collection_build_lock(self.collection_name)
            with build_lock:
                if not self.chunks and self.collection_name:
                    # 双检的第二处：另一个 Retriever 可能已经把索引发布到进程级缓存，
                    # 那就直接复用，别再全量 scroll 一次。
                    with _bm25_cache_lock:
                        cached = _bm25_cache.get(self.collection_name)
                    if cached is not None:
                        self.chunks, self.bm25 = cached
                        self._bm25_version = _bm25_version(self.collection_name)
                        self._bm25_ready = True
                        return
                    store = BM25Store()
                    try:
                        if store.has_chunks(self.collection_name):
                            self.chunks = store.load_chunks(self.collection_name)
                        else:
                            self.chunks = _load_all_chunks(self.collection_name)
                            store.save_chunks(self.collection_name, self.chunks)
                    finally:
                        store.close()
                if self.chunks:
                    tokenized = [_tokenize(c.text) for c in self.chunks]
                    self.bm25 = BM25Okapi(tokenized)
                    if self.collection_name:
                        with _bm25_cache_lock:
                            _bm25_cache[self.collection_name] = (self.chunks, self.bm25)
                            self._bm25_version = _bm25_version(self.collection_name)
                else:
                    self.bm25 = None
                self._bm25_ready = True

    def _refresh_bm25_if_stale(self) -> None:
        self._ensure_bm25()
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

    def retrieve(self, query: str, top_k: int = 5, doc_name: str = None, tags: list[str] = None, weights: dict[str, float] = None, allowed_version_ids: set[int] = None, validity_statuses: set[str] = None, allowed_wiki_revision_ids: set[int] = None) -> list[Chunk]:
        """Dense + sparse retrieval, fused by RRF.

        ``allowed_version_ids`` scopes **document** chunks (``document_versions.id``), while
        ``allowed_wiki_revision_ids`` scopes **wiki** chunks (wiki revision ids). They are
        separate id spaces that can collide, so they are never merged into one authorization
        set (S4 review, 2026-10-22). ``None`` means "this domain is unrestricted"; a version
        filter without a wiki scope excludes wiki chunks (fail-closed).
        """
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
                    allowed_version_ids=_version_scope_filter(
                        allowed_version_ids, allowed_wiki_revision_ids
                    ),
                    validity_statuses=validity_statuses,
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

        # tags：dense 侧由 Qdrant payload 过滤，sparse 侧此前完全没有过滤——tag 条件在
        # 密集检索抖动时会静默失效（A-04）。这里用与 dense 相同的语义（要求包含全部 tag）；
        # 没有 tag 数据的块视为不匹配（与"Qdrant 里没有该 tag"一致）。
        if tags:
            required = {str(tag) for tag in tags}
            sparse_hits = [c for c in sparse_hits if required <= set(decode_tags(c))]

        # 版本过滤：两个 id 空间各自匹配；BM25 快照若缺少版本元数据则整体禁用，避免泄漏历史内容。
        scope_active = (
            allowed_version_ids is not None or allowed_wiki_revision_ids is not None
        )
        if scope_active:
            if self.chunks and getattr(self.chunks[0], "document_version_id", None) is not None:
                sparse_hits = [
                    c
                    for c in sparse_hits
                    if _in_version_scope(c, allowed_version_ids, allowed_wiki_revision_ids)
                ]
            else:
                sparse_hits = []
        if validity_statuses:
            if self.chunks and getattr(self.chunks[0], "validity_status", None) is not None:
                sparse_hits = [c for c in sparse_hits if c.validity_status in validity_statuses]
            else:
                sparse_hits = []
        # dense 侧：Qdrant 只能按合并后的 document_version_id 过滤，跨域重号必须在拿到结果后
        # 按 chunk_kind 再判一次（S4 的串权用例走的就是这条路）。
        if scope_active:
            dense_hits = [
                c
                for c in dense_hits
                if _in_version_scope(c, allowed_version_ids, allowed_wiki_revision_ids)
            ]

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
            replace(
                representatives[key],
                retrieval_score=scores[key],
                rerank_score=None,
            )
            for key in sorted(scores, key=lambda key: -scores[key])[:top_k]
        ]
