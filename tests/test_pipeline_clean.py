"""Tests that RAGPipeline.__init__ calls clean_document and passes cleaned text to chunker."""

from unittest.mock import patch

from rag.models import Chunk


@patch("rag.pipeline.Reranker")
@patch("rag.pipeline.clear")
@patch("rag.pipeline.Retriever")
@patch("rag.pipeline.add")
@patch("rag.pipeline.embed")
@patch("rag.pipeline.chunk")
@patch("rag.pipeline.clean_document")
@patch("rag.pipeline.load")
def test_pipeline_calls_clean_document(
    mock_load, mock_clean, mock_chunk, mock_embed,
    mock_add, mock_retriever_cls, mock_clear, mock_reranker_cls,
):
    """clean_document must be called once during pipeline init."""
    mock_load.return_value = "raw text with​ zero-width spaces"
    mock_clean.return_value = ("clean text", {"title": "Doc"})
    mock_chunk.return_value = [
        Chunk(text="chunk1", doc_name="test.txt", chunk_index=0),
    ]
    mock_embed.return_value = [[0.1] * 1024]

    from rag.pipeline import RAGPipeline
    RAGPipeline("test.txt")

    mock_clean.assert_called_once_with("raw text with​ zero-width spaces")


@patch("rag.pipeline.Reranker")
@patch("rag.pipeline.clear")
@patch("rag.pipeline.Retriever")
@patch("rag.pipeline.add")
@patch("rag.pipeline.embed")
@patch("rag.pipeline.chunk")
@patch("rag.pipeline.clean_document")
@patch("rag.pipeline.load")
def test_pipeline_passes_cleaned_text_to_chunker(
    mock_load, mock_clean, mock_chunk, mock_embed,
    mock_add, mock_retriever_cls, mock_clear, mock_reranker_cls,
):
    """The cleaned text (not the raw text) must be forwarded to chunk()."""
    mock_load.return_value = "raw text"
    mock_clean.return_value = ("cleaned text", {"author": "Alice"})
    mock_chunk.return_value = [
        Chunk(text="chunk1", doc_name="test.txt", chunk_index=0),
    ]
    mock_embed.return_value = [[0.1] * 1024]

    from rag.pipeline import RAGPipeline
    RAGPipeline("test.txt")

    # chunk must receive the cleaned text, not the raw text
    mock_chunk.assert_called_once()
    call_args = mock_chunk.call_args
    assert call_args[0][0] == "cleaned text", (
        f"Expected cleaned text passed to chunk, got {call_args[0][0]!r}"
    )
