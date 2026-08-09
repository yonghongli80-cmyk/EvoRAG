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


def add_to_collection(collection_name: str, chunks: list[Chunk], embeddings: list[list[float]], tags: list[str] = None, doc_permission_id: int = None):
    with _write_lock.write():
        client = _get_client()
        if not client.collection_exists(collection_name):
            raise ValueError(f"集合 {collection_name} 不存在，请先创建知识库")
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=embeddings[i],
                payload={
                    "text": chunks[i].text,
                    "doc_name": chunks[i].doc_name,
                    "chunk_index": chunks[i].chunk_index,
                    "tags": tags or [],
                    **({"doc_permission_id": doc_permission_id} if doc_permission_id is not None else {}),
                },
            )
            for i in range(len(chunks))
        ]
        client.upsert(collection_name=collection_name, points=points)
    _invalidate_bm25(collection_name)


def search_collection(collection_name: str, query_embedding: list[float], top_k: int = 5, doc_name: str = None, tags: list[str] = None) -> list[Chunk]:
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
            )
            for h in hits.points
            if h.payload
        ]


def delete_doc(collection_name: str, doc_name: str):
    from qdrant_client.models import FieldCondition, MatchValue

    with _write_lock.write():
        client = _get_client()
        client.delete(
            collection_name=collection_name,
            points_selector=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))]),
        )
    _invalidate_bm25(collection_name)
