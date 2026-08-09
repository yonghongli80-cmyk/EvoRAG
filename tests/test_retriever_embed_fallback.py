"""Dense embedding failures should degrade retrieval to BM25."""

from unittest.mock import patch

from rag.models import Chunk
from rag.retriever import Retriever


def test_retrieve_returns_filtered_bm25_results_when_embedding_fails():
    chunks = [
        Chunk(text="alpha handbook", doc_name="alpha.md", chunk_index=0),
        Chunk(text="beta notes", doc_name="beta.md", chunk_index=0),
    ]
    retriever = Retriever(chunks)

    with patch("rag.retriever.embed", side_effect=RuntimeError("embedding offline")), patch(
        "rag.retriever.dense_search"
    ) as dense_search:
        results = retriever.retrieve("alpha", top_k=2, doc_name="alpha.md")

    dense_search.assert_not_called()
    assert [chunk.doc_name for chunk in results] == ["alpha.md"]
