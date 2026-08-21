import os
import tempfile
from unittest.mock import MagicMock, patch

from rag.pipeline import RAGPipeline


@patch("rag.agent.route_question", return_value="rag")
@patch("rag.pipeline.rewrite_query", return_value="What is RAG?")
@patch("rag.pipeline.Reranker")
@patch("rag.pipeline.add")
@patch("rag.pipeline.Retriever")
@patch("rag.pipeline.clear")
@patch("rag.embedder.client")
@patch("rag.generator.client")
def test_e2e_full_pipeline(mock_gen_client, mock_emb_client, mock_clear, mock_retriever_cls, mock_add, mock_reranker_cls, mock_rewrite, mock_route):
    """Full integration test: load → chunk → embed → store → retrieve → generate."""
    mock_emb_response = MagicMock()
    mock_emb_response.data = [MagicMock(embedding=[0.1] * 1024) for _ in range(3)]
    mock_emb_client.embeddings.create.return_value = mock_emb_response

    mock_retriever = MagicMock()
    mock_retriever_cls.return_value = mock_retriever

    mock_reranker = MagicMock()
    mock_reranker_cls.return_value = mock_reranker

    mock_message = MagicMock()
    mock_message.content = "RAG stands for Retrieval Augmented Generation."
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_gen_response = MagicMock()
    mock_gen_response.choices = [mock_choice]
    mock_gen_client.chat.completions.create.return_value = mock_gen_response

    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write(
            "Retrieval Augmented Generation (RAG) is a technique in AI.\n"
            "It combines retrieval with text generation.\n"
            "RAG is used for question answering tasks.\n"
        )
        tmp_path = f.name

    try:
        pipeline = RAGPipeline(tmp_path)
        mock_retriever.retrieve.return_value = pipeline.chunks
        mock_reranker.rerank.return_value = pipeline.chunks
        result = pipeline.query("What is RAG?")

        assert result.answer == "RAG stands for Retrieval Augmented Generation."
        assert len(pipeline.chunks) > 0
        assert pipeline.retriever is not None
        mock_emb_client.embeddings.create.assert_called()
        mock_reranker.rerank.assert_called_once()
        mock_gen_client.chat.completions.create.assert_called_once()
    finally:
        os.unlink(tmp_path)
