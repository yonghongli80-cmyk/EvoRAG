"""Empty retrieval must pass the evidence gate even outside enterprise mode.

Workflow A (R-1): the legacy path used to call ``generate`` whenever the request was
non-enterprise, even when retrieval returned nothing. That bypassed the evidence gate
and let the model answer from an empty context. The Jev path already refused in that
case (``build_bundle=enterprise_request or not context``); these tests pin the aligned
legacy behavior and the non-empty fallback that must stay unchanged.
"""

from unittest.mock import MagicMock, patch

from rag.models import Chunk
from rag.pipeline import RAGPipeline
from rag.reliability import fixed_decision_copy


def _build_pipeline(*, kb_id=None, user_db=None):
    """Build a pipeline with the indexing/retrieval/rerank boundaries stubbed."""
    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.delete_doc"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker") as reranker_cls:
        retriever_cls.return_value = MagicMock()
        reranker_cls.return_value = MagicMock()
        feedback_processor = MagicMock()
        feedback_processor.get_weights.return_value = {}
        feedback_processor.get_weight.return_value = None
        return RAGPipeline(
            "test.txt",
            kb_id=kb_id,
            user_db=user_db,
            session_id="s1",
            memory_db_path=":memory:",
            feedback_processor=feedback_processor,
            gap_analyzer=MagicMock(),
        )


@patch("rag.agent.route_question", return_value="rag")
@patch("rag.pipeline.rewrite_query", return_value="rewritten question")
@patch("rag.pipeline.generate")
def test_non_enterprise_empty_context_refuses_without_generate(
    mock_generate, mock_rewrite, mock_route
):
    """No kb_id + empty retrieval: fixed insufficient-evidence refusal, no generation."""
    pipeline = _build_pipeline()
    pipeline.retriever.retrieve.return_value = []
    pipeline.reranker.rerank.return_value = []
    try:
        with patch.object(pipeline.tracker, "save") as mock_save:
            result = pipeline.query("资料里没有的问题")
    finally:
        pipeline.close()

    trace = mock_save.call_args[0][0]
    assert trace.route == "fixed"
    assert result.decision is not None
    assert result.decision.kind == "insufficient_evidence"
    assert result.answer == fixed_decision_copy("insufficient_evidence")
    assert result.context == []
    assert result.sources == []
    mock_generate.assert_not_called()
    mock_route.assert_called_once()
    mock_rewrite.assert_called_once()


@patch("rag.agent.route_question", return_value="rag")
@patch("rag.pipeline.rewrite_query", return_value="rewritten question")
@patch("rag.pipeline.generate", return_value="generated answer")
def test_non_enterprise_non_empty_context_still_generates(
    mock_generate, mock_rewrite, mock_route
):
    """No kb_id + non-empty retrieval: the unchanged legacy path still generates."""
    pipeline = _build_pipeline()
    chunk = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    pipeline.retriever.retrieve.return_value = [chunk]
    pipeline.reranker.rerank.return_value = [chunk]
    try:
        result = pipeline.query("年假有几天")
    finally:
        pipeline.close()

    assert result.answer == "generated answer"
    assert result.decision is None
    assert result.context == [chunk]
    assert [source["doc_name"] for source in result.sources] == ["policy.md"]
    mock_generate.assert_called_once()
    mock_rewrite.assert_called_once()
    mock_route.assert_called_once()


@patch("rag.agent.route_question", return_value="rag")
@patch("rag.pipeline.rewrite_query", return_value="rewritten question")
@patch("rag.pipeline.generate")
def test_enterprise_empty_context_still_refuses(mock_generate, mock_rewrite, mock_route):
    """kb_id set + empty retrieval: enterprise mode keeps refusing without generation."""
    user = {"id": 7, "permission_level": 1, "is_admin": False}
    user_db = MagicMock()
    user_db.get_accessible_doc_names.return_value = []
    user_db.get_document_permission.return_value = None
    user_db.list_document_versions.return_value = []
    user_db.get_document_permissions_by_names.return_value = {}

    pipeline = _build_pipeline(kb_id="rag_docs", user_db=user_db)
    pipeline.retriever.retrieve.return_value = []
    pipeline.reranker.rerank.return_value = []
    try:
        with patch.object(pipeline.tracker, "save") as mock_save:
            result = pipeline.query("年假是多少", user=user)
    finally:
        pipeline.close()

    trace = mock_save.call_args[0][0]
    assert trace.route == "fixed"
    assert result.decision is not None
    assert result.decision.kind == "insufficient_evidence"
    assert result.answer == fixed_decision_copy("insufficient_evidence")
    assert result.context == []
    assert result.sources == []
    mock_generate.assert_not_called()
    mock_rewrite.assert_called_once()
    mock_route.assert_called_once()
