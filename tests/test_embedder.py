from unittest.mock import MagicMock, patch

from rag.embedder import embed


@patch("rag.embedder.client")
def test_embed_single_text(mock_client):
    mock_response = MagicMock()
    mock_response.data = [MagicMock(embedding=[0.1] * 1024)]
    mock_client.embeddings.create.return_value = mock_response

    result = embed(["hello"])
    assert len(result[0]) == 1024
    mock_client.embeddings.create.assert_called_once()


@patch("rag.embedder.client")
def test_embed_multiple_texts(mock_client):
    mock_response = MagicMock()
    mock_response.data = [
        MagicMock(embedding=[0.1, 0.2]),
        MagicMock(embedding=[0.3, 0.4]),
    ]
    mock_client.embeddings.create.return_value = mock_response

    result = embed(["a", "b"])
    assert len(result) == 2
