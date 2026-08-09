from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_add_tags_to_file_reads_and_updates_points_in_batches():
    import rag.api as api

    client = MagicMock()
    client.collection_exists.return_value = True
    first = MagicMock(id="id1")
    second = MagicMock(id="id2")
    client.scroll.return_value = ([first, second], None)
    old_first = MagicMock(payload={"tags": ["old_tag"]})
    old_second = MagicMock(payload={"tags": ["shared"]})
    client.retrieve.return_value = [old_first, old_second]

    with patch.object(api, "_get_current_user", return_value={"id": 1}), patch(
        "rag.permissions.check_doc_permission"
    ), patch("rag.vector_store._get_client", return_value=client):
        result = await api.add_tags_to_file(
            "test.txt", ["shared", "new_tag"], authorization="Bearer token"
        )

    assert result["points_updated"] == 2
    client.retrieve.assert_called_once_with(
        collection_name="rag_docs",
        ids=["id1", "id2"],
        with_payload=True,
    )
    client.set_payload.assert_called_once_with(
        collection_name="rag_docs",
        payload={"tags": ["new_tag", "old_tag", "shared"]},
        points=["id1", "id2"],
    )
