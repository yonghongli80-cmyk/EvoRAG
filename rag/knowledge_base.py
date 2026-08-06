"""多知识库管理模块"""

import re
import uuid
from dataclasses import dataclass

from config import settings
from rag.models import Chunk


def _slugify(name: str) -> str:
    """Convert display name to a safe slug for collection naming."""
    slug = re.sub(r"[^a-zA-Z0-9一-鿿]", "_", name).strip("_").lower()
    return slug[:16] if slug else "kb"


@dataclass
class KnowledgeBaseInfo:
    kb_id: str
    name: str
    doc_count: int
    created_at: str


class KnowledgeBaseManager:
    def __init__(self):
        from rag.vector_store import _get_client

        self._client = _get_client()

    def create_kb(self, name: str) -> str:
        from qdrant_client.models import Distance, VectorParams

        slug = _slugify(name)
        kb_id = f"kb_{slug}_{uuid.uuid4().hex[:6]}"
        self._client.create_collection(
            collection_name=kb_id,
            vectors_config=VectorParams(
                size=settings.embed_dimension,
                distance=Distance.COSINE,
            ),
        )
        return kb_id

    def list_kbs(self) -> list[KnowledgeBaseInfo]:
        collections = self._client.get_collections().collections
        result = []
        for c in collections:
            if not c.name.startswith("kb_"):
                continue
            count = self._client.count(collection_name=c.name).count
            # Extract display name: kb_slug_hexid → slug (strip kb_ prefix and _hex suffix)
            display_name = c.name[3:-7] if len(c.name) > 10 else c.name[3:]
            result.append(
                KnowledgeBaseInfo(
                    kb_id=c.name,
                    name=display_name,
                    doc_count=count,
                    created_at="",
                )
            )
        return result

    def delete_kb(self, kb_id: str) -> None:
        if not kb_id.startswith("kb_"):
            raise ValueError(f"不能删除系统集合 {kb_id}，只能删除 kb_ 前缀的知识库")
        if not self._client.collection_exists(kb_id):
            raise ValueError(f"知识库 {kb_id} 不存在")
        self._client.delete_collection(kb_id)

    def add_document(self, kb_id: str, file_path: str, doc_name: str = None, doc_permission_id: int = None, document_id: int = None, document_version_id: int = None, version_no: int = None) -> int:
        import time

        from rag.chunker import chunk
        from rag.embedder import embed
        from rag.loader import load
        from rag.vector_store import add_to_collection

        text = load(file_path)
        if doc_name is None:
            doc_name = file_path.split("/")[-1].split("\\")[-1]
        chunks = chunk(text, doc_name=doc_name)
        embeddings = embed([c.text for c in chunks])
        add_to_collection(
            kb_id,
            chunks,
            embeddings,
            doc_permission_id=doc_permission_id,
            document_id=document_id,
            document_version_id=document_version_id,
            version_no=version_no,
            updated_at=time.time(),
        )
        return len(chunks)

    def remove_document(self, kb_id: str, doc_name: str) -> None:
        from rag.vector_store import delete_doc

        delete_doc(kb_id, doc_name)

    def search(self, kb_id: str, query: str, top_k: int = 5) -> list[Chunk]:
        from rag.embedder import embed
        from rag.vector_store import search_collection

        query_vec = embed([query])[0]
        return search_collection(kb_id, query_vec, top_k=top_k)
