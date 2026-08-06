import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, Filter, PointStruct, VectorParams

from config import settings
from rag.concurrency import ReadWriteLock
from rag.models import Chunk

COLLECTION_NAME = "rag_docs"

_client = None
_write_lock = ReadWriteLock()


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
                },
            )
            for i in range(len(chunks))
        ]
        client.upsert(collection_name=COLLECTION_NAME, points=points)


def clear():
    """Delete all points from the collection, then recreate it fresh."""
    with _write_lock.write():
        client = _get_client()
        if client.collection_exists(COLLECTION_NAME):
            client.delete_collection(COLLECTION_NAME)
        _ensure_collection()


def search(query_embedding: list[float], top_k: int = 5) -> list[Chunk]:
    with _write_lock.read():
        client = _get_client()
        _ensure_collection()
        hits = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_embedding,
            limit=top_k,
            with_payload=True,
        )
        return [
            Chunk(
                text=h.payload["text"],
                doc_name=h.payload.get("doc_name", ""),
                chunk_index=h.payload.get("chunk_index", 0),
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


def add_to_collection(collection_name: str, chunks: list[Chunk], embeddings: list[list[float]], tags: list[str] = None, doc_permission_id: int = None, document_id: int = None, document_version_id: int = None, version_no: int = None, is_current: bool = True, validity_status: str = "valid", section_title: str = None, updated_at: float = None):
    with _write_lock.write():
        client = _get_client()
        if not client.collection_exists(collection_name):
            raise ValueError(f"集合 {collection_name} 不存在，请先创建知识库")
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
            points.append(
                PointStruct(id=str(uuid.uuid4()), vector=embeddings[i], payload=payload)
            )
        client.upsert(collection_name=collection_name, points=points)


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
