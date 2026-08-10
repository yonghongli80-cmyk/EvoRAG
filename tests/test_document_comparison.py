import pytest

from rag.reliability.comparison import (
    ComparisonCitationDraft,
    ComparisonRequest,
    DocumentEvidence,
    ToolTimeout,
)
from rag.reliability.llama_workflow import compare_documents
from rag.reliability.models import ComparisonScope


def _citation(document_id: int, version_id: int = 11) -> ComparisonCitationDraft:
    return ComparisonCitationDraft(
        document_id=document_id,
        document_version_id=version_id,
        chunk_index=0,
        section_title="Leave",
        text_preview="five days",
        rank=0,
    )


def _scope(*document_ids: int) -> ComparisonScope:
    return ComparisonScope(
        actor_id=7,
        allowed_document_ids=frozenset(document_ids),
        permission_scope_fingerprint="scope-v1",
    )


class FakeDocumentTool:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def query_document(self, document_id, question, context):
        self.calls.append((document_id, question, context))
        response = self.responses[document_id]
        if isinstance(response, BaseException):
            raise response
        return response


def test_document_evidence_requires_location_citation_drafts():
    citation = _citation(1)

    evidence = DocumentEvidence(document_id=1, text="policy", citations=(citation,))

    assert evidence.citations == (citation,)


@pytest.mark.asyncio
async def test_comparison_never_fills_missing_side():
    fake_document_tool = FakeDocumentTool(
        {
            1: DocumentEvidence(document_id=1, text="Policy A", citations=(_citation(1),)),
            2: ToolTimeout(),
        }
    )
    request = ComparisonRequest(
        question="compare leave policy",
        document_ids=(1, 2),
        dimensions=("leave",),
    )

    result = await compare_documents(request, _scope(1, 2), fake_document_tool)

    assert result.partial is True
    assert result.rows[0].values[1].text == "Policy A"
    assert result.rows[0].values[1].citations == (_citation(1),)
    assert result.rows[0].values[2].status == "insufficient_evidence"
    assert result.rows[0].values[2].text == ""
    assert result.rows[0].values[2].citations == ()
    assert fake_document_tool.calls[0][2] == _scope(1, 2)


@pytest.mark.asyncio
async def test_comparison_rejects_mismatched_citation_document():
    tool = FakeDocumentTool({
        1: DocumentEvidence(document_id=1, text="Policy A", citations=(_citation(2),)),
    })

    result = await compare_documents(
        ComparisonRequest(question="compare", document_ids=(1,), dimensions=()),
        _scope(1),
        tool,
    )

    assert result.partial is True
    assert result.rows[0].values[1].status == "insufficient_evidence"
    assert result.rows[0].values[1].citations == ()


@pytest.mark.asyncio
async def test_comparison_rejects_out_of_scope_document_before_tool_call():
    fake_document_tool = FakeDocumentTool({})
    request = ComparisonRequest(question="compare", document_ids=(1, 2), dimensions=())

    with pytest.raises(PermissionError, match="scope denied"):
        await compare_documents(request, _scope(1), fake_document_tool)

    assert fake_document_tool.calls == []


@pytest.mark.asyncio
async def test_comparison_rejects_more_than_five_documents_before_tool_call():
    fake_document_tool = FakeDocumentTool({})
    request = ComparisonRequest(
        question="compare",
        document_ids=(1, 2, 3, 4, 5, 6),
        dimensions=(),
    )

    with pytest.raises(ValueError, match="document limit"):
        await compare_documents(request, _scope(1, 2, 3, 4, 5, 6), fake_document_tool)

    assert fake_document_tool.calls == []
