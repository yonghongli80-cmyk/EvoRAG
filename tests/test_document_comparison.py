import pytest

from rag.reliability.comparison import ComparisonRequest, DocumentEvidence, ToolTimeout
from rag.reliability.llama_workflow import compare_documents
from rag.reliability.models import ComparisonScope


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


@pytest.mark.asyncio
async def test_comparison_never_fills_missing_side():
    fake_document_tool = FakeDocumentTool(
        {
            1: DocumentEvidence(document_id=1, text="制度 A", citation_ids=(11,)),
            2: ToolTimeout(),
        }
    )
    request = ComparisonRequest(
        question="比较制度",
        document_ids=(1, 2),
        dimensions=("年假",),
    )
    scope = ComparisonScope(
        actor_id=7,
        allowed_document_ids=frozenset({1, 2}),
        permission_scope_fingerprint="scope-v1",
    )

    result = await compare_documents(request, scope, fake_document_tool)

    assert result.partial is True
    assert result.rows[0].values[1].text == "制度 A"
    assert result.rows[0].values[1].citation_ids == (11,)
    assert result.rows[0].values[2].status == "insufficient_evidence"
    assert result.rows[0].values[2].text == ""
    assert result.rows[0].values[2].citation_ids == ()
    assert fake_document_tool.calls[0][2] == scope


@pytest.mark.asyncio
async def test_comparison_rejects_out_of_scope_document_before_tool_call():
    fake_document_tool = FakeDocumentTool({})
    request = ComparisonRequest(question="比较制度", document_ids=(1, 2), dimensions=())
    scope = ComparisonScope(
        actor_id=7,
        allowed_document_ids=frozenset({1}),
        permission_scope_fingerprint="scope-v1",
    )

    with pytest.raises(PermissionError, match="scope denied"):
        await compare_documents(request, scope, fake_document_tool)

    assert fake_document_tool.calls == []


@pytest.mark.asyncio
async def test_comparison_rejects_more_than_five_documents_before_tool_call():
    fake_document_tool = FakeDocumentTool({})
    request = ComparisonRequest(
        question="比较制度",
        document_ids=(1, 2, 3, 4, 5, 6),
        dimensions=(),
    )
    scope = ComparisonScope(
        actor_id=7,
        allowed_document_ids=frozenset({1, 2, 3, 4, 5, 6}),
        permission_scope_fingerprint="scope-v1",
    )

    with pytest.raises(ValueError, match="document limit"):
        await compare_documents(request, scope, fake_document_tool)

    assert fake_document_tool.calls == []
