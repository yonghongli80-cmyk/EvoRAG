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


@patch("rag.embedder.client")
def test_embed_batches_uncached_texts_and_reuses_cached(mock_client):
    from rag.embedder import clear_cache
    clear_cache()

    # First call caches "cached_text"
    resp1 = MagicMock()
    resp1.data = [MagicMock(embedding=[1.0, 1.0])]
    mock_client.embeddings.create.return_value = resp1
    res1 = embed(["cached_text"])
    assert res1 == [[1.0, 1.0]]
    assert mock_client.embeddings.create.call_count == 1

    # Second call embeds ["cached_text", "new1", "new2"]
    resp2 = MagicMock()
    resp2.data = [MagicMock(embedding=[2.0, 2.0]), MagicMock(embedding=[3.0, 3.0])]
    mock_client.embeddings.create.return_value = resp2

    res2 = embed(["cached_text", "new1", "new2"])
    assert res2 == [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    # Only 1 additional call made, with input=["new1", "new2"]
    assert mock_client.embeddings.create.call_count == 2
    last_call = mock_client.embeddings.create.call_args
    assert last_call[1]["input"] == ["new1", "new2"]

