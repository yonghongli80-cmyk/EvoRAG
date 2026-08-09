import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rag.models import Chunk
from rag.pipeline import RAGPipeline
from rag.retriever import Retriever


def test_pipeline_feedback_weights_change_rrf_candidate_order():
    low_feedback = Chunk(text="low-feedback", doc_name="guide.md", chunk_index=0)
    high_feedback = Chunk(text="high-feedback", doc_name="guide.md", chunk_index=1)
    weights = {
        hashlib.md5(low_feedback.text.encode()).hexdigest(): 0.2,
        hashlib.md5(high_feedback.text.encode()).hexdigest(): 2.0,
    }

    pipeline = RAGPipeline.__new__(RAGPipeline)
    pipeline._cache = MagicMock()
    pipeline._cache.get.return_value = None
    pipeline._kb_id = "rag_docs"
    pipeline._user_db = None
    pipeline.session_id = "session"
    pipeline.memory = MagicMock()
    pipeline.memory.build_messages.return_value = []
    pipeline._feedback_processor = MagicMock()
    pipeline._feedback_processor.get_weight.side_effect = weights.get
    pipeline.reranker = MagicMock()
    pipeline.reranker.rerank.side_effect = lambda _query, chunks: chunks
    pipeline.retriever = MagicMock()
    pipeline.retriever.retrieve.side_effect = lambda _query, top_k, **kwargs: Retriever._rrf_fuse(
        [low_feedback, high_feedback], [], top_k, weights=kwargs.get("weights")
    )

    allowed = SimpleNamespace(blocked=False, reason="")
    with patch("rag.agent.route_question", return_value="rag"), patch(
        "rag.pipeline.rewrite_query", return_value="rewritten"
    ), patch("rag.pipeline.check_injection", return_value=allowed):
        prepared, error = pipeline._prepare_context("question", "session", None, top_k=2)

    assert error is None
    assert [chunk.text for chunk in prepared["context"]] == [
        "high-feedback",
        "low-feedback",
    ]
    assert pipeline._feedback_processor.get_weight.call_count == 2
