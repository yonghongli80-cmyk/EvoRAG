from unittest.mock import MagicMock

import pytest

from rag.models import Chunk
from rag.reliability.document_tool import CurrentVersionDocumentEvidenceTool
from rag.reliability.models import ComparisonScope


def _scope(document_id: int, version_id: int) -> ComparisonScope:
    return ComparisonScope(
        actor_id=7,
        allowed_document_ids=frozenset({document_id}),
        allowed_document_versions=frozenset({(document_id, version_id)}),
        permission_scope_fingerprint="scope-v1",
    )


@pytest.mark.asyncio
async def test_tool_retrieves_only_authorized_current_version():
    retriever = MagicMock()
    retriever.retrieve.return_value = [Chunk(
        text="five days",
        doc_name="policy.pdf",
        chunk_index=0,
        document_id=1,
        document_version_id=11,
        section_title="Leave",
    )]
    db = MagicMock()
    db.get_document_permission_by_id.return_value = {
        "id": 1, "kb_id": "rag_docs", "doc_name": "policy.pdf", "current_version_id": 11,
    }
    tool = CurrentVersionDocumentEvidenceTool(db, {"rag_docs": retriever})

    evidence = await tool.query_document(1, "leave", _scope(1, 11))

    assert evidence.citations[0].document_version_id == 11
    assert retriever.retrieve.call_args.kwargs["allowed_version_ids"] == {11}


@pytest.mark.asyncio
async def test_tool_returns_no_evidence_when_current_version_changed():
    retriever = MagicMock()
    db = MagicMock()
    db.get_document_permission_by_id.return_value = {
        "id": 1, "kb_id": "rag_docs", "doc_name": "policy.pdf", "current_version_id": 12,
    }
    tool = CurrentVersionDocumentEvidenceTool(db, {"rag_docs": retriever})

    evidence = await tool.query_document(1, "leave", _scope(1, 11))

    assert evidence.text == ""
    assert evidence.citations == ()
    retriever.retrieve.assert_not_called()


@pytest.mark.asyncio
async def test_tool_rejects_out_of_scope_document_without_retrieval():
    retriever = MagicMock()
    tool = CurrentVersionDocumentEvidenceTool(MagicMock(), {"rag_docs": retriever})

    with pytest.raises(PermissionError, match="scope denied"):
        await tool.query_document(2, "leave", _scope(1, 11))

    retriever.retrieve.assert_not_called()
