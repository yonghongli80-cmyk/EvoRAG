from rag.models import Chunk
from rag.retriever import Retriever


def test_rrf_fuse_deduplicates_chunks_that_only_differ_by_permission_id():
    dense_chunk = Chunk(
        text="quarterly revenue was 10 million",
        doc_name="finance.pdf",
        chunk_index=3,
        doc_permission_id=42,
    )
    sparse_chunk = Chunk(
        text="quarterly revenue was 10 million",
        doc_name="finance.pdf",
        chunk_index=3,
    )

    results = Retriever._rrf_fuse([dense_chunk], [sparse_chunk], top_k=2)

    assert results == [dense_chunk]
    assert results[0].doc_permission_id == 42
