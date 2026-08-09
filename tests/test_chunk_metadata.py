from unittest.mock import MagicMock, patch

from rag.chunker import chunk
from rag.cleaner import clean_document


def test_cleaned_document_metadata_is_retained_in_vector_payload():
    cleaned, metadata = clean_document("# Quarterly Report\nAuthor: Alice\nDate: 2026-08-01\nBody")
    chunks = chunk(cleaned, doc_name="report.md", metadata=metadata)

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    with patch("rag.vector_store._get_client", return_value=mock_client):
        from rag.vector_store import add

        add(chunks, [[0.1] * 1024 for _ in chunks])

    payload = mock_client.upsert.call_args.kwargs["points"][0].payload
    assert payload["metadata"] == {
        "title": "Quarterly Report",
        "author": "Alice",
        "date": "2026-08-01",
    }
