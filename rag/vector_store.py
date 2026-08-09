import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, Filter, PointStruct, VectorParams

from config import settings
from rag.concurrency import ReadWriteLock
from rag.models import Chunk

COLLECTION_NAME = "rag_docs"

_client = None
_write_lock = ReadWriteLock()


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
        _client = QdrantClient(path=settings.qdrant_path)
    return _client


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
        for chunk, embedding in zip(chunks, embeddings)
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
        return [
            Chunk(
                text=h.payload["text"],
                doc_name=h.payload.get("doc_name", ""),
                chunk_index=h.payload.get("chunk_index", 0),
                metadata=h.payload.get("metadata", {}),
            )
            for h in hits.points
            if h.payload
        ]


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
            if c_permission_id is not None:
                payload["doc_permission_id"] = c_permission_id
            if c_document_id is not None:
                payload["document_id"] = c_document_id
            if c_version_id is not None:
                payload["document_version_id"] = c_version_id
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
        return [
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
            )
            for h in hits.points
            if h.payload
        ]


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


def delete_doc(collection_name: str, doc_name: str):
    from qdrant_client.models import FieldCondition, MatchValue

    with _write_lock.write():
        client = _get_client()
        client.delete(
            collection_name=collection_name,
            points_selector=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))]),
        )
    _invalidate_bm25(collection_name)
