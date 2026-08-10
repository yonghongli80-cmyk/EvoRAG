"""Current-version-only retrieval adapter for document comparison."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from rag.reliability.comparison import ComparisonCitationDraft, DocumentEvidence
from rag.reliability.models import ComparisonScope


class CurrentVersionDocumentEvidenceTool:
    """Retrieve one pre-authorized, authorization-time document version."""

    def __init__(self, user_db: Any, retrievers: Mapping[str, Any]) -> None:
        self._user_db = user_db
        self._retrievers = retrievers

    async def query_document(
        self, document_id: int, question: str, context: ComparisonScope
    ) -> DocumentEvidence:
        if document_id not in context.allowed_document_ids:
            raise PermissionError("document comparison scope denied")
        version_id = context.version_for(document_id)
        if version_id is None:
            return DocumentEvidence(document_id=document_id)
        document = self._user_db.get_document_permission_by_id(document_id)
        if document is None or document.get("current_version_id") != version_id:
            return DocumentEvidence(document_id=document_id)
        retriever = self._retrievers.get(document["kb_id"])
        if retriever is None:
            raise RuntimeError("comparison retrieval backend unavailable")
        chunks = await asyncio.to_thread(
            retriever.retrieve,
            question,
            top_k=1,
            doc_name=document["doc_name"],
            allowed_version_ids={version_id},
        )
        matching_chunks = [
            chunk
            for chunk in chunks
            if chunk.document_id == document_id and chunk.document_version_id == version_id
        ]
        if not matching_chunks:
            return DocumentEvidence(document_id=document_id)
        citations = tuple(
            ComparisonCitationDraft(
                document_id=document_id,
                document_version_id=version_id,
                chunk_index=chunk.chunk_index,
                section_title=chunk.section_title or "",
                text_preview=chunk.text[:200],
                rank=rank,
            )
            for rank, chunk in enumerate(matching_chunks)
        )
        return DocumentEvidence(
            document_id=document_id,
            text="\n".join(chunk.text for chunk in matching_chunks),
            citations=citations,
        )
