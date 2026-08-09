from unittest.mock import patch

from rag.models import Chunk


def test_default_collection_dense_search_applies_document_and_tag_filters():
    from rag.retriever import Retriever

    allowed = Chunk(text="allowed", doc_name="target.txt", chunk_index=0)
    leaked = Chunk(text="leaked", doc_name="other.txt", chunk_index=0)
    retriever = Retriever([])

    with patch("rag.retriever.embed", return_value=[[0.1] * 1024]), patch(
        "rag.retriever.dense_search", return_value=[allowed, leaked]
    ) as dense_search:
        result = retriever.retrieve(
            "question", top_k=5, doc_name="target.txt", tags=["finance"]
        )

    dense_search.assert_called_once_with(
        [0.1] * 1024,
        top_k=10,
        doc_name="target.txt",
        tags=["finance"],
    )
    assert result == [allowed]
