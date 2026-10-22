import threading
import time
import uuid
from collections import deque

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from config import settings
from rag.concurrency import ReadWriteLock
from rag.models import Chunk

COLLECTION_NAME = "rag_docs"

_SCROLL_PAGE = 256

# 检索阶段的进程内指标：只保留计数与最近的耗时样本（秒），供只读 /metrics 聚合。
# 不记录查询文本、文档名或集合名，避免把业务数据带进指标端点。
_SEARCH_SAMPLE_LIMIT = 512

_client = None
_write_lock = ReadWriteLock()

_search_lock = threading.Lock()
_search_counts = {"hit": 0, "empty": 0}
_search_samples: deque[float] = deque(maxlen=_SEARCH_SAMPLE_LIMIT)


def client_mode() -> str:
    """Return ``"remote"`` when a Qdrant server URL is configured, else ``"local"``."""
    return "remote" if (settings.qdrant_url or "").strip() else "local"


def _record_search(duration_seconds: float, result_count: int) -> None:
    with _search_lock:
        _search_counts["hit" if result_count else "empty"] += 1
        _search_samples.append(max(0.0, duration_seconds))


def search_stats_snapshot() -> dict:
    """Return a copy of the search counters/samples for metrics aggregation."""
    with _search_lock:
        return {"counts": dict(_search_counts), "samples": list(_search_samples)}


def reset_search_stats() -> None:
    """Reset the process-local search metrics (used by tests and for clean windows)."""
    with _search_lock:
        _search_counts["hit"] = 0
        _search_counts["empty"] = 0
        _search_samples.clear()


def _invalidate_bm25(collection_name: str) -> None:
    """Remove the sparse snapshot after the authoritative vector data changes."""
    from rag.bm25_store import BM25Store

    store = BM25Store()
    try:
        store.invalidate(collection_name)
    finally:
        store.close()


def _get_client():
    global _client
    if _client is None:
        url = (settings.qdrant_url or "").strip()
        if url:
            # Phase 1：连 Qdrant server，所有副本共享同一份向量数据。
            api_key = (settings.qdrant_api_key or "").strip() or None
            _client = QdrantClient(url=url, api_key=api_key)
        else:
            # 默认（向后兼容）：本地嵌入式目录，进程级独占锁。
            _client = QdrantClient(path=settings.qdrant_path)
    return _client


def close_client() -> None:
    """Release the process-local Qdrant lock held by the shared client."""
    global _client
    client, _client = _client, None
    if client is not None:
        client.close()


def delete_unversioned_points(collection_name: str, doc_name: str) -> int:
    """Delete points for a document that predate document versioning.

    Pre-versioning writes (for example the folder indexer on a fresh volume) leave points
    without ``document_version_id``; once a versioned write exists those stale points must go
    so retrieval does not return the same chunk twice.
    """
    with _write_lock.write():
        client = _get_client()
        if not client.collection_exists(collection_name):
            return 0
        stale: list = []
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=collection_name,
                scroll_filter=Filter(
                    must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))]
                ),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=True,
            )
            stale.extend(
                record.id for record in records
                if not (record.payload or {}).get("document_version_id")
            )
            if offset is None:
                break
        if not stale:
            return 0
        client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=stale),
        )
        _invalidate_bm25(collection_name)
        return len(stale)


def _ensure_collection():
    client = _get_client()
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=settings.embed_dimension,
                distance=Distance.COSINE,
            ),
        )


def add(chunks: list[Chunk], embeddings: list[list[float]]):
    with _write_lock.write():
        client = _get_client()
        _ensure_collection()
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=embeddings[i],
                payload={
                    "text": chunks[i].text,
                    "doc_name": chunks[i].doc_name,
                    "chunk_index": chunks[i].chunk_index,
                    "metadata": chunks[i].metadata,
                },
            )
            for i in range(len(chunks))
        ]
        client.upsert(collection_name=COLLECTION_NAME, points=points)
    _invalidate_bm25(COLLECTION_NAME)


def replace(chunks: list[Chunk], embeddings: list[list[float]]) -> None:
    """Replace the default collection and restore the old points if writing fails."""
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must have the same length")

    new_points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embedding,
            payload={
                "text": chunk.text,
                "doc_name": chunk.doc_name,
                "chunk_index": chunk.chunk_index,
                "metadata": chunk.metadata,
            },
        )
        for chunk, embedding in zip(chunks, embeddings, strict=False)
    ]

    with _write_lock.write():
        client = _get_client()
        had_collection = client.collection_exists(COLLECTION_NAME)
        old_points: list[PointStruct] = []

        if had_collection:
            offset = None
            while True:
                records, offset = client.scroll(
                    collection_name=COLLECTION_NAME,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
                old_points.extend(
                    PointStruct(
                        id=record.id,
                        vector=record.vector,
                        payload=record.payload or {},
                    )
                    for record in records
                )
                if offset is None:
                    break

        try:
            if had_collection:
                client.delete_collection(COLLECTION_NAME)
            _ensure_collection()
            if new_points:
                client.upsert(collection_name=COLLECTION_NAME, points=new_points)
        except Exception as write_error:
            try:
                if client.collection_exists(COLLECTION_NAME):
                    client.delete_collection(COLLECTION_NAME)
                if had_collection:
                    _ensure_collection()
                    if old_points:
                        client.upsert(collection_name=COLLECTION_NAME, points=old_points)
            except Exception as rollback_error:
                raise RuntimeError(
                    "vector collection replacement failed and rollback failed"
                ) from rollback_error
            raise write_error

    _invalidate_bm25(COLLECTION_NAME)


def clear():
    """Delete all points from the collection, then recreate it fresh."""
    with _write_lock.write():
        client = _get_client()
        if client.collection_exists(COLLECTION_NAME):
            client.delete_collection(COLLECTION_NAME)
        _ensure_collection()
    _invalidate_bm25(COLLECTION_NAME)


def search(
    query_embedding: list[float],
    top_k: int = 5,
    doc_name: str = None,
    tags: list[str] = None,
) -> list[Chunk]:
    started = time.perf_counter()
    with _write_lock.read():
        client = _get_client()
        _ensure_collection()
        conditions = []
        if doc_name:
            from qdrant_client.models import FieldCondition, MatchValue

            conditions.append(FieldCondition(key="doc_name", match=MatchValue(value=doc_name)))
        if tags:
            from qdrant_client.models import FieldCondition, MatchValue

            conditions.extend(
                FieldCondition(key="tags", match=MatchValue(value=tag)) for tag in tags
            )
        hits = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_embedding,
            limit=top_k,
            with_payload=True,
            query_filter=Filter(must=conditions) if conditions else None,
        )
        results = [
            Chunk(
                text=h.payload["text"],
                doc_name=h.payload.get("doc_name", ""),
                chunk_index=h.payload.get("chunk_index", 0),
                metadata=h.payload.get("metadata", {}),
            )
            for h in hits.points
            if h.payload
        ]
    _record_search(time.perf_counter() - started, len(results))
    return results


def _ensure_collection_name(collection_name: str):
    client = _get_client()
    if not client.collection_exists(collection_name):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=settings.embed_dimension,
                distance=Distance.COSINE,
            ),
        )


def add_to_collection(
    collection_name: str,
    chunks: list[Chunk],
    embeddings: list[list[float]],
    tags: list[str] = None,
    doc_permission_id: int = None,
    document_id: int = None,
    document_version_id: int = None,
    version_no: int = None,
    is_current: bool = True,
    validity_status: str = "valid",
    section_title: str = None,
    updated_at: float = None,
):
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must have the same length")

    from qdrant_client.models import FieldCondition, MatchValue, PointIdsList
    with _write_lock.write():
        client = _get_client()
        if not client.collection_exists(collection_name):
            raise ValueError(f"集合 {collection_name} 不存在，请先创建知识库")
        versioned_write = document_version_id is not None or any(
            chunk.document_version_id is not None for chunk in chunks
        )
        existing_ids = set()
        if not versioned_write:
            for doc_name in {chunk.doc_name for chunk in chunks}:
                offset = None
                while True:
                    records, offset = client.scroll(
                        collection_name=collection_name,
                        limit=10000,
                        offset=offset,
                        scroll_filter=Filter(
                            must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))]
                        ),
                        with_payload=False,
                    )
                    existing_ids.update(record.id for record in records)
                    if offset is None:
                        break

        points = []
        for i, chunk in enumerate(chunks):
            # Explicit kwargs win; otherwise derive from the chunk itself so
            # callers can pass plain versioned chunks without repeating fields.
            c_document_id = document_id if document_id is not None else chunk.document_id
            c_version_id = (
                document_version_id
                if document_version_id is not None
                else chunk.document_version_id
            )
            c_version_no = version_no if version_no is not None else chunk.version_no
            c_is_current = is_current if is_current is not None else chunk.is_current
            c_validity = validity_status if validity_status is not None else chunk.validity_status
            c_section = section_title if section_title is not None else chunk.section_title
            c_updated = updated_at if updated_at is not None else chunk.updated_at
            c_permission_id = (
                doc_permission_id
                if doc_permission_id is not None
                else chunk.doc_permission_id
            )

            payload = {
                "text": chunk.text,
                "doc_name": chunk.doc_name,
                "chunk_index": chunk.chunk_index,
                "tags": tags or [],
                "metadata": chunk.metadata,
                "is_current": bool(c_is_current),
                "validity_status": c_validity,
            }
            for key, value in {
                "dataset_id": chunk.dataset_id,
                "sheet_id": chunk.sheet_id,
                "sheet_key": chunk.sheet_key,
                "sheet_name": chunk.sheet_name,
                "chunk_kind": chunk.chunk_kind,
                "row_start": chunk.row_start,
                "row_end": chunk.row_end,
                "column_keys": list(chunk.column_keys) if chunk.column_keys else None,
            }.items():
                if value is not None:
                    payload[key] = value
            if c_permission_id is not None:
                payload["doc_permission_id"] = c_permission_id
            if c_document_id is not None:
                payload["document_id"] = c_document_id
            if c_version_id is not None:
                payload["document_version_id"] = c_version_id
                payload["version_id"] = c_version_id
            if c_version_no is not None:
                payload["version_no"] = c_version_no
            if c_section is not None:
                payload["section_title"] = c_section
            if c_updated is not None:
                payload["updated_at"] = c_updated
            point_id = (
                str(uuid.uuid4())
                if versioned_write
                else str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection_name}:{chunk.doc_name}:{chunk.chunk_index}"))
            )
            points.append(PointStruct(id=point_id, vector=embeddings[i], payload=payload))
        client.upsert(collection_name=collection_name, points=points)
        if not versioned_write:
            stale_ids = existing_ids - {point.id for point in points}
            if stale_ids:
                client.delete(
                    collection_name=collection_name,
                    points_selector=PointIdsList(points=list(stale_ids)),
                )
    _invalidate_bm25(collection_name)


def search_collection(collection_name: str, query_embedding: list[float], top_k: int = 5, doc_name: str = None, tags: list[str] = None, allowed_version_ids: set[int] = None, validity_statuses: set[str] = None) -> list[Chunk]:
    started = time.perf_counter()
    with _write_lock.read():
        client = _get_client()
        if not client.collection_exists(collection_name):
            return []

        conditions = []
        if doc_name:
            from qdrant_client.models import FieldCondition, MatchValue
            conditions.append(FieldCondition(key="doc_name", match=MatchValue(value=doc_name)))
        if tags:
            from qdrant_client.models import FieldCondition, MatchValue
            for tag in tags:
                conditions.append(FieldCondition(key="tags", match=MatchValue(value=tag)))
        if allowed_version_ids:
            from qdrant_client.models import FieldCondition, MatchAny
            conditions.append(
                FieldCondition(key="document_version_id", match=MatchAny(any=sorted(allowed_version_ids)))
            )
        if validity_statuses:
            from qdrant_client.models import FieldCondition, MatchAny
            conditions.append(
                FieldCondition(key="validity_status", match=MatchAny(any=sorted(validity_statuses)))
            )

        search_filter = Filter(must=conditions) if conditions else None

        hits = client.query_points(
            collection_name=collection_name,
            query=query_embedding,
            limit=top_k,
            with_payload=True,
            query_filter=search_filter,
        )
        results = [
            Chunk(
                text=h.payload["text"],
                doc_name=h.payload.get("doc_name", ""),
                chunk_index=h.payload.get("chunk_index", 0),
                doc_permission_id=h.payload.get("doc_permission_id"),
                metadata=h.payload.get("metadata", {}),
                document_id=h.payload.get("document_id"),
                document_version_id=h.payload.get("document_version_id"),
                version_no=h.payload.get("version_no"),
                is_current=bool(h.payload.get("is_current", True)),
                validity_status=h.payload.get("validity_status", "valid"),
                section_title=h.payload.get("section_title"),
                updated_at=h.payload.get("updated_at"),
                dataset_id=h.payload.get("dataset_id"),
                sheet_id=h.payload.get("sheet_id"),
                sheet_key=h.payload.get("sheet_key"),
                sheet_name=h.payload.get("sheet_name"),
                chunk_kind=h.payload.get("chunk_kind"),
                row_start=h.payload.get("row_start"),
                row_end=h.payload.get("row_end"),
                column_keys=tuple(h.payload.get("column_keys", ())),
            )
            for h in hits.points
            if h.payload
        ]
    _record_search(time.perf_counter() - started, len(results))
    return results


def mark_document_version_current(collection_name: str, document_id: int, version_id: int) -> None:
    """Repair the is_current flag so only the promoted version is marked current."""
    from qdrant_client.models import FieldCondition, MatchValue

    with _write_lock.write():
        client = _get_client()
        if not client.collection_exists(collection_name):
            return
        client.set_payload(
            collection_name=collection_name,
            payload={"is_current": False},
            points=Filter(
                must=[
                    FieldCondition(key="document_id", match=MatchValue(value=document_id)),
                    FieldCondition(key="is_current", match=MatchValue(value=True)),
                ]
            ),
        )
        client.set_payload(
            collection_name=collection_name,
            payload={"is_current": True},
            points=Filter(
                must=[FieldCondition(key="document_version_id", match=MatchValue(value=version_id))]
            ),
        )


def delete_document_version(collection_name: str, version_id: int) -> None:
    """Delete only the vectors written for one document version."""
    from qdrant_client.models import FieldCondition, MatchValue

    with _write_lock.write():
        client = _get_client()
        client.delete(
            collection_name=collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="document_version_id", match=MatchValue(value=version_id))]
            ),
        )


def copy_units_to_version(
    collection_name: str,
    *,
    source_version_id: int,
    target_version_id: int,
    document_id: int,
    version_no: int,
    unit_keys: list[str] | tuple[str, ...],
    dataset_id: int | None = None,
) -> int:
    """Clone exact unchanged semantic units into an immutable target version."""
    from qdrant_client.models import FieldCondition, MatchValue

    requested = set(unit_keys)
    if not requested:
        return 0
    with _write_lock.write():
        client = _get_client()
        if not client.collection_exists(collection_name):
            return 0
        records = []
        offset = None
        source_filter = Filter(
            must=[
                FieldCondition(
                    key="document_id", match=MatchValue(value=document_id)
                ),
                FieldCondition(
                    key="document_version_id",
                    match=MatchValue(value=source_version_id),
                ),
            ]
        )
        while True:
            batch, offset = client.scroll(
                collection_name=collection_name,
                scroll_filter=source_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            records.extend(batch)
            if offset is None:
                break

        selected = {}
        for record in records:
            payload = record.payload or {}
            unit_key = (payload.get("metadata") or {}).get("unit_key")
            if unit_key in requested:
                if unit_key in selected or record.vector is None:
                    return 0
                selected[unit_key] = record
        if set(selected) != requested:
            return 0

        points = []
        for unit_key in unit_keys:
            record = selected[unit_key]
            payload = dict(record.payload or {})
            payload.update(
                document_id=document_id,
                document_version_id=target_version_id,
                version_no=version_no,
                is_current=True,
            )
            if dataset_id is not None:
                payload["dataset_id"] = dataset_id
            points.append(
                PointStruct(
                    id=str(uuid.uuid4()), vector=record.vector, payload=payload
                )
            )
        client.upsert(collection_name=collection_name, points=points)
    _invalidate_bm25(collection_name)
    return len(points)


def delete_doc(collection_name: str, doc_name: str, chunk_kind: str | None = None):
    from qdrant_client.models import FieldCondition, MatchValue

    with _write_lock.write():
        client = _get_client()
        if not client.collection_exists(collection_name):
            return
        conditions = [FieldCondition(key="doc_name", match=MatchValue(value=doc_name))]
        if chunk_kind is not None:
            conditions.append(FieldCondition(key="chunk_kind", match=MatchValue(value=chunk_kind)))
        client.delete(
            collection_name=collection_name,
            points_selector=Filter(must=conditions),
        )
    _invalidate_bm25(collection_name)
