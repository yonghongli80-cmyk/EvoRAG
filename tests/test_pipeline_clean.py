"""Tests that RAGPipeline.__init__ calls clean_document and passes cleaned text to chunker."""

from unittest.mock import patch

from config import settings
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


def _initialize_pipeline_with_real_chunks(tmp_path, monkeypatch, text):
    import rag.pipeline as pipeline_module
    from rag.chunker import chunk as real_chunk

    source = tmp_path / "dedup.txt"
    source.write_text(text, encoding="utf-8")
    monkeypatch.setattr(settings, "chunk_size", 120)
    monkeypatch.setattr(settings, "chunk_overlap", 1)
    original_chunks = []
    embedded_texts = []
    indexed_chunks = []

    def capture_chunks(*args, **kwargs):
        chunks = real_chunk(*args, **kwargs)
        original_chunks.extend(chunks)
        return chunks

    def capture_embeddings(texts):
        embedded_texts.append(texts)
        return [[float(i)] for i, _ in enumerate(texts)]

    def capture_indexed_chunks(chunks, embeddings):
        indexed_chunks.append(chunks)

    monkeypatch.setattr(pipeline_module, "chunk", capture_chunks)
    monkeypatch.setattr(pipeline_module, "embed", capture_embeddings)
    monkeypatch.setattr(pipeline_module, "add", capture_indexed_chunks)
    monkeypatch.setattr(pipeline_module, "delete_doc", lambda collection, name: None)
    monkeypatch.setattr(pipeline_module, "Retriever", lambda chunks, **kwargs: object())
    monkeypatch.setattr(pipeline_module, "Reranker", lambda: object())

    instance = pipeline_module.RAGPipeline(str(source), memory_db_path=":memory:")
    return instance, original_chunks, embedded_texts, indexed_chunks


def test_pipeline_dedup_keeps_first_chunk_identity_and_metadata(tmp_path, monkeypatch):
    repeated = "# First title\n" + ("First paragraph keeps its source identity. " * 2)
    distinct = "# Second title\n" + ("A separate paragraph remains after deduplication. " * 2)
    pipeline, original_chunks, embedded_texts, indexed_chunks = _initialize_pipeline_with_real_chunks(
        tmp_path, monkeypatch, f"{repeated}\n\n{repeated}\n\n{distinct}"
    )

    assert [chunk.text for chunk in pipeline.chunks] == [
        original_chunks[0].text,
        original_chunks[-1].text,
    ]
    assert pipeline.chunks[0] is original_chunks[0]
    assert pipeline.chunks[0].chunk_index == 0
    assert pipeline.chunks[0].metadata == {"title": "First title"}
    assert pipeline.chunks[1].chunk_index == 2
    assert embedded_texts == [[original_chunks[0].text, original_chunks[-1].text]]
    assert indexed_chunks[0] == pipeline.chunks
    assert indexed_chunks[0][0] is original_chunks[0]


def test_pipeline_dedup_removes_near_duplicate_chunks(tmp_path, monkeypatch):
    first = "A" * 100 + "x"
    near_duplicate = "A" * 100 + "y"
    pipeline, original_chunks, embedded_texts, indexed_chunks = _initialize_pipeline_with_real_chunks(
        tmp_path, monkeypatch, f"{first}\n\n{near_duplicate}"
    )

    assert len(original_chunks) == 2
    assert pipeline.chunks == [original_chunks[0]]
    assert embedded_texts == [[original_chunks[0].text]]
    assert indexed_chunks[0] == [original_chunks[0]]


def test_pipeline_dedup_accepts_empty_chunk_list(tmp_path, monkeypatch):
    pipeline, original_chunks, embedded_texts, indexed_chunks = _initialize_pipeline_with_real_chunks(
        tmp_path, monkeypatch, ""
    )

    assert original_chunks == []
    assert pipeline.chunks == []
    assert embedded_texts == [[]]
    assert indexed_chunks == [[]]
